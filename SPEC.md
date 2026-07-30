# Build Spec — Hybrid VPC Lattice Landing Zone (Python CDK + CDK Pipelines)

> **How to use this file:** Open your project folder in VS Code, start Claude Code, and paste this entire
> file as your first message (or save it as `SPEC.md` in the repo root and tell Claude Code:
> *"Read SPEC.md and build the whole thing, stack by stack. Ask me before deploying."*).
> Fill in every `<<FILL_ME>>` first — they're listed in section 0.

---

## 0. Fill these in before you start

| Placeholder | Value |
|---|---|
| `<<AWS_ACCOUNT_ID>>` | `458798438816` |
| `<<REGION>>` | `us-east-1` |
| `<<GITHUB_OWNER>>` | your GitHub username/org |
| `<<GITHUB_REPO>>` | e.g. `lattice-landing-zone` |
| `<<GITHUB_BRANCH>>` | `main` |
| `<<CODECONNECTIONS_ARN>>` | ARN of a GitHub connection (create once — see §7) |
| `<<WEBAPP_SOURCE>>` | path/URL of the web app I already built (static SPA and/or a server on a port) |
| `<<SECOND_ACCOUNT_ID>>` | optional — only if you have a 2nd account for the RAM cross-account demo |

**Assumptions if I don't say otherwise:** single AWS account, single-AZ for cost (code written so a
`MULTI_AZ` flag flips to 2 AZs), everything tagged `Project=lattice-lab`, and **every** resource gets
`RemovalPolicy.DESTROY` + `autoDeleteObjects` so `cdk destroy --all` leaves nothing behind.

---

## 1. Role & objective

You are building a **hands-on lab that mirrors an enterprise "Lattice-first" hybrid landing zone** so I can
deploy it, tear it down, and explain every piece in an interview. It must:

- Be **100% AWS CDK in Python** — no console clicking to create resources.
- Deploy and tear down **entirely from git** via a **self-mutating CDK Pipeline** sourced from GitHub.
- Exercise **every VPC Lattice feature** (section 5 is the priority — this is the focus).
- Also include **Transit Gateway, Gateway Load Balancer, PrivateLink**, a **three-tier web app**
  (CloudFront/S3 web tier → ALB + Auto Scaling app tier → RDS data tier), and a **Site-to-Site VPN**
  standing in for Direct Connect.
- Tag **every** resource and expose them through **AWS Resource Groups** (section 3.5).
- **HA inspection:** the security/inspection VPC is **multi-AZ with two firewall appliances per AZ**.
- Be built and **tested in layers** — each stack deploys and is verifiable on its own.
- Keep cost low elsewhere: single-AZ for non-HA tiers, `t3.micro`/`t3.small`, one NAT, tear-down-friendly.

Work **incrementally**: scaffold → one stack at a time → `cdk synth` after each → summarize what you built
and how to verify it → wait for my go-ahead before `cdk deploy`. Never run `cdk deploy` without asking.

---

## 2. Toolchain (do this first, on macOS)

Verify/instal: Homebrew, `awscli`, `node`, `python@3.12`, `git`, and `npm i -g aws-cdk`. Create a Python
venv, `pip install aws-cdk-lib>=2.170.0 constructs cdk-nag`. **Pin a recent `aws-cdk-lib`** — the VPC Lattice
**resource gateway** and **resource configuration** L1 constructs only exist in newer versions; if
`aws_cdk.aws_vpclattice.CfnResourceGateway` / `CfnResourceConfiguration` are missing, upgrade `aws-cdk-lib`
until they resolve, and print the version you landed on. Credentials: a dedicated IAM admin user
(`deloitte-admin`, in the `deloitte` group with `AdministratorAccess`) already exists in this account,
configured locally as CLI profile `deloitte` (`~/.aws/credentials`) — use `--profile deloitte` /
`AWS_PROFILE=deloitte` for every AWS CLI and CDK command below rather than creating a new IAM user. Then
`cdk bootstrap aws://<<AWS_ACCOUNT_ID>>/<<REGION>> --profile deloitte`.

---

## 3. Repo layout

```
lattice-landing-zone/
├── app.py                      # CDK app entry — instantiates the pipeline
├── cdk.json
├── requirements.txt
├── SPEC.md                     # this file
├── README.md                   # deploy/teardown/verify instructions you generate
├── docs/
│   └── inspection-architecture-reference.md  # GWLB/TGW symmetry, Linux firewall stack, diagram grid spec — read before writing inspection_stack.py / diagram_stack.py
├── diagrams/                   # cdk-dia / infra-composer exports land here
├── diagram-site/                # source for the hand-built architecture diagram page
│   └── index.html               # single self-contained SVG diagram (see §6, diagram_stack.py)
├── config.py                   # env, CIDRs, AZ count, feature flags, tag map
├── app/                        # my web app (<<WEBAPP_SOURCE>>) — you containerize/serve it
└── stacks/
    ├── network_stack.py        # VPCs + Transit Gateway + route tables + VPN + on-prem
    ├── inspection_stack.py     # GWLB + firewall appliances (2 per AZ, multi-AZ) + endpoints + appliance-mode
    ├── threetier_stack.py      # web tier (CloudFront+S3) → app tier (ALB+ASG) → data tier (RDS)
    ├── privatelink_stack.py    # provider NLB + endpoint service + consumer interface endpoint
    ├── lattice_stack.py        # ALL Lattice features (section 5) — the centerpiece
    ├── kafka_stack.py          # real KRaft Kafka on EC2 (deployed last, feature-flagged off first)
    ├── observability_stack.py  # log groups, access logs, metrics dashboards
    ├── resource_groups_stack.py# tag-based AWS Resource Groups (section 3.5)
    └── diagram_stack.py        # S3 (private, OAC) + CloudFront hosting for diagram-site/index.html
```

Use a top-level `LandingZoneStage(Stage)` that instantiates the stacks in dependency order so the **pipeline
deploys the whole stage**. Pass shared objects (VPCs, TGW, service network) between stacks via stage-level
props/constructs, not by re-lookup.

---

## 3.5 Tagging & AWS Resource Groups (resource_groups_stack.py)

- Apply a **consistent tag set to every resource** via `cdk.Tags.of(app).add(...)` at the app level, plus a
  per-stack `Layer` tag. Minimum tags: `Project=lattice-lab`, `Environment=lab`, `Owner=<<GITHUB_OWNER>>`,
  `Layer=<network|inspection|threetier|privatelink|lattice|kafka|observability|diagram>`, `CostCenter=interview-lab`,
  `ManagedBy=cdk`. Keep the tag map in `config.py` so it's one source of truth.
- Create **tag-based AWS Resource Groups** (`aws_cdk.aws_resourcegroups.CfnGroup`) with a
  `TAG_FILTERS_1_0` resource query: one **umbrella group** matching `Project=lattice-lab`, and **one group per
  `Layer`** so I can open a group in the console and see exactly that layer's resources. This also makes
  teardown auditing and cost allocation trivial. Verify the groups populate after deploy.

## 4. Network + transit + hybrid (network_stack.py)

- **Four VPCs**, single-AZ by default (flag `MULTI_AZ`):
  - `onprem-vpc` **10.100.0.0/16** — the simulated datacenter.
  - `inspection-vpc` **10.0.0.0/16**.
  - `app-vpc` (spoke) **10.1.0.0/16**.
  - `provider-vpc` **10.2.0.0/16**.
  Each with public + private subnets; **one NAT gateway** total (put internet-needing things in `app-vpc`).
- **Transit Gateway** with `default_route_table_association=disable`, `default_route_table_propagation=disable`
  so you manage routing explicitly. Create **two TGW route tables**: `spoke-rt` and `inspection-rt`.
  Attach all four VPCs; use **appliance mode** on the inspection attachment.
- **Segmentation:** spokes route `0.0.0.0/0` and inter-spoke traffic **into the inspection VPC** (appliance
  mode keeps flows symmetric); inspection RT routes back out to the spokes and to on-prem.
- **Site-to-Site VPN as the DX stand-in:** in `onprem-vpc`, launch a **libreswan** EC2 (Amazon Linux 2023,
  user-data installs & configures libreswan as the IPsec peer) as the **Customer Gateway**. Create
  `CfnCustomerGateway` (its EIP), `CfnVpnConnection` to the **TGW** with **static routes** to `10.100.0.0/16`,
  and put the tunnel PSK/config into the libreswan user-data. Add TGW route-table routes for `10.100.0.0/16`
  via the VPN attachment. (Provide the two AWS tunnel outside IPs + PSKs to the appliance via user-data —
  read them from the `CfnVpnConnection` attributes.)
- **Placeholder broker** for the pilot: an EC2 in `onprem-vpc` private subnet running a TCP listener on
  **9092** (`ncat -k -l 9092` via user-data). This is swapped for real Kafka in `kafka_stack.py` later.
- Enable **SSM** on all EC2s (instance profile + no SSH) so I can Session-Manager into them to test.

**Verify:** from an SSM session on the app-vpc test host, `nc -vz <onprem-broker-ip> 9092` succeeds over the VPN.

---

## 5. VPC LATTICE — the centerpiece (lattice_stack.py)

Implement **every** feature below. Group the code with clear comments `# ---- L4a ----` etc. so I can read it.

**L4a — Service network + first service**
- `CfnServiceNetwork` (`auth_type=AWS_IAM`).
- `CfnService` with an **HTTP listener** (`CfnListener`), default `CfnRule`, and an **INSTANCE** `CfnTargetGroup`
  pointing at the app-tier ASG instances. Health checks configured.
- `CfnServiceNetworkServiceAssociation` (service → network) and
  `CfnServiceNetworkVpcAssociation` (app-vpc → network) so the consumer can resolve it.

**L4b — All target-group types + rich rules**
- Target groups of type **IP**, **LAMBDA** (a tiny inline Lambda), and **ALB** (front the app-tier ALB).
- Listener rules: **path-based** (`/v1/*` → tgA), **header-based** (`x-canary: true` → tgB), and a
  **weighted** rule (90/10 canary between v1/v2 target groups).
- Set **AZ affinity** on target groups where supported.

**L4c — HTTPS + custom domain + ACM + TLS passthrough**
- An **HTTPS listener** with an **ACM certificate** (request/lookup a cert) and a **custom domain name** on
  the service. (Use a domain you control or a self-signed/ACM private CA for the lab; document which.)
- A **second service using TLS_PASSTHROUGH** listener protocol for end-to-end encryption.

**L4d — Auth policies (zero-trust) + SG-on-association + managed prefix list**
- **Service-network auth policy** AND **service-level auth policy** (`CfnAuthPolicy`), IAM/SigV4, with a
  condition allowing only a specific principal / `aws:SourceVpc` / `aws:PrincipalOrgID`.
- Attach a **security group to the VPC association** that references the **VPC Lattice managed prefix list**
  (`com.amazonaws.<<REGION>>.vpc-lattice`) for ingress — demonstrate defense-in-depth.
- Provide a test script that curls the service **with and without** SigV4 signing to show allow vs deny.

**L4e — Resource gateway + resource configuration + service-network endpoint (hybrid)**
- `CfnResourceGateway` with ENIs in a VPC that has the **TGW+VPN path to on-prem**, plus a security group.
- `CfnResourceConfiguration` of type **single** pointing at the **on-prem broker** (IP or domain, port 9092),
  associated to the service network (`CfnServiceNetworkResourceAssociation`).
- A **service-network VPC endpoint (PrivateLink-powered)** so a consumer can reach the service network via an
  endpoint instead of a full VPC association — this is the Lattice↔PrivateLink tie-in.
- **Verify:** the app-vpc consumer resolves the Lattice-managed DNS name for the resource and reaches the
  on-prem broker **through the resource gateway over the VPN**.

**L4f — Observability + dual-stack + optional RAM share**
- **Access logs** on the service network AND a service, delivered to **CloudWatch Logs** and **S3**.
- A **dual-stack / IPv6** target group + service to demonstrate IPv4↔IPv6 bridging (ties to the IPv6 story).
- **Optional (needs `<<SECOND_ACCOUNT_ID>>`):** share the service network / resource config via **AWS RAM**
  (`CfnResourceShare`). If single-account, generate the RAM code but guard it behind a flag and print the
  cross-account grant that *would* be created.

---

## 6. Other layers

**inspection_stack.py (GWLB, HA):** read `docs/inspection-architecture-reference.md` §4–5 before writing this
file — it resolves the exact TGW/route-table/health-check pitfalls that silently break this pattern. Summary:

- The security/inspection VPC spans **two AZs**. `CfnLoadBalancer` type `gateway`, target group protocol
  **GENEVE/6081**, target type `instance`, with **Appliance Mode enabled on the target group** — this is
  mandatory, not optional, or GWLB can hash the two directions of one flow to different targets. **Two
  appliances per AZ (4 total)**, one ASG per AZ (`min=max=desired=2`), both appliances in that AZ's ASG
  registered to that AZ's GWLB target group.
- **Also enable `appliance_mode_support=ENABLE` on the Inspection VPC's Transit Gateway attachment** — a
  *separate* setting from the GWLB target-group Appliance Mode above. Both must be on; either alone still
  breaks flow symmetry.
- Health check: TCP against the firewall software's own health-check port (below) — **never GENEVE** as the
  check protocol.
- **GWLB endpoints in each AZ** (`CfnVPCEndpoint` type `GatewayLoadBalancer`), plus an **IGW ingress
  (edge-association) route table** redirecting return traffic for NAT'd/public IPs back into the GWLB
  endpoints — without this, inbound reply packets skip the firewall on the reverse leg. This is the piece
  most commonly missed; see the reference doc §4.3.
- **Firewall software (the actual "Linux firewall mimicking a PL"):** don't build a passthrough stub.
  User-data/AMI installs three things: (1) **`aws-gateway-load-balancer-tunnel-handler` (gwlbtun)** — AWS's
  open-source GENEVE decap/encap daemon, terminating GWLB's tunnel and exposing a native health-check
  listener (point the GWLB health check at this); (2) **nftables**, configured as a zone-based policy engine
  (named zone chains, rendered from a `policy.yaml` policy-as-code file — this *is* the "PL," Palo-Alto-style
  source-zone/dest-zone/service/action rulebase, minus any vendor product) for east-west and north-south
  filtering; (3) **Suricata** in inline IPS mode via NFQUEUE (ET Open ruleset) for threat inspection on
  traffic the PL flags for it. Full nftables table structure, the policy.yaml schema, and the
  policy-as-code sync pipeline are in the reference doc §5.
- Do **not** disable `source_dest_check` on the primary GWLB-facing ENI (GENEVE packets target the ENI's own
  IP:6081, not a forwarded destination — this is a common but incorrect carryover from NAT-instance patterns;
  reference doc §4.7/§5.4). Verify MTU end-to-end with an actual large-packet test, not just a config review.
- Verify traffic hits appliances in both AZs, and that killing one appliance is detected and replaced.

**threetier_stack.py (three-tier web app):**
- **Web tier:** **S3 + CloudFront** serving the static frontend (`<<WEBAPP_SOURCE>>`); a **CloudFront VPC
  origin** to the private app-tier ALB for the dynamic API (fallback: public ALB custom origin — document the
  trade-off).
- **App tier:** internal **ALB** → **Auto Scaling group** running the app server (`<<WEBAPP_SOURCE>>`;
  containerize or run via user-data, document the port). This ALB is reused as the Lattice **ALB target
  group** in L4b.
- **Data tier:** an **RDS** instance (single-AZ lab; `MULTI_AZ` flag) in private subnets, reachable only from
  the app-tier security group. Store credentials in **Secrets Manager**. This is the classic three-tier
  separation — web can't touch the DB, only the app tier can.

**privatelink_stack.py:** provider app in `provider-vpc` behind an **NLB**, exposed as a **VPC endpoint
service** (`CfnVPCEndpointService`); an **interface endpoint** in `app-vpc` consumes it. Show it's one-way and
overlapping-CIDR-safe.

**kafka_stack.py (deploy LAST, flag `ENABLE_KAFKA=False` at first):** self-managed **Kafka in KRaft mode**
(no ZooKeeper) on an EC2 in `onprem-vpc` (user-data installs Kafka, formats storage with `kafka-storage.sh`,
starts broker on 9092, advertised listener = its private IP). Repoint the L4e resource configuration at this
broker. Provide a producer/consumer test that runs from the app-vpc consumer **through the Lattice mesh**.

**diagram_stack.py (hosted architecture diagram):** read `docs/inspection-architecture-reference.md` §6 before
building `diagram-site/index.html` — it has the exact node/edge inventory, SVG grid system, and box-sizing
table; don't re-derive these from scratch. Summary:

- A single self-contained `diagram-site/index.html` — the whole diagram body is **one SVG on a fixed 8px-unit
  coordinate grid** (not flexbox-drifted HTML boxes), so every box origin, connector vertex, and arrowhead is
  provably aligned rather than eyeballed. Connectors are **orthogonal only** (90° bends, no diagonals),
  entering the firewall boxes on dedicated edges per flow class (north-south on top, east-west on
  side/bottom) so the two flow types never visually converge on the same edge.
- Depicts: Internet → IGW → inspection VPC → GWLB → firewall fleet (one swimlane per AZ, both appliances per
  AZ) → TGW → spokes/on-prem, plus a visually separated, unconnected "how this page is delivered" mini-panel
  for CloudFront/S3 so it's never mistaken for part of the network itself.
- North-south vs. east-west traffic color-coded via CSS custom properties (not hardcoded hex) with a real
  HTML legend (not SVG text, for accessibility/reflow), WCAG AA contrast in both themes, and a
  non-color differentiator (line style) for colorblind viewers.
- **Do not label the firewall boxes "Palo Alto" or any other vendor/product name — anywhere** (box title, sub-label,
  code comment, or SVG `<title>`/`<desc>`). Title the box "Inspection Firewall (Linux EC2)." Underneath, in
  the *exact* text below — **uppercase + letter-spacing, not CSS small-caps** (small-caps is a no-op on
  already-uppercase source text and was deliberately ruled out):
  ```
  NFTABLES ZONE POLICY (PL) + SURICATA IPS
  GWLB GENEVE TUNNEL HANDLER
  ```
  (single-line fallback if it doesn't fit the box at build time: `NFTABLES + SURICATA IPS ON GWLB GENEVE TUNNEL`
  — widen the box in 8px increments before shrinking the font or inventing a shorter label; see reference §6.6.)
- Responsive (no body horizontal scroll; the SVG itself gets its own scroll fallback below a legibility
  floor), light/dark mode via `prefers-color-scheme`, fully inline (no external CDN/font/JS calls — it sits
  behind a strict CloudFront CSP with `script-src 'none'`).
- Deploy: a **private S3 bucket** (Block Public Access all on, static-website-hosting feature **off** — that
  mode doesn't support OAC) served **only** via **CloudFront with Origin Access Control (OAC)**, bucket policy
  scoped to `cloudfront.amazonaws.com` with an `AWS:SourceArn` condition (no legacy OAI), HTTPS-only (redirect
  HTTP→HTTPS), default root object `index.html`. Same `RemovalPolicy.DESTROY` + `autoDeleteObjects` as
  everything else. Print the CloudFront URL after deploy so I can open the page. Confirm with me before adding
  a custom domain or any public-access restriction (WAF/signed URLs) — default is the plain `*.cloudfront.net`
  URL, open behind the OAC-only access pattern.

---

## 7. CDK Pipeline from GitHub (app.py)

- Use `aws_cdk.pipelines.CodePipeline` with
  `CodePipelineSource.connection("<<GITHUB_OWNER>>/<<GITHUB_REPO>>", "<<GITHUB_BRANCH>>",
  connection_arn="<<CODECONNECTIONS_ARN>>")`.
- Synth step: `pip install -r requirements.txt && cdk synth`.
- Add the `LandingZoneStage` to the pipeline so **push-to-git = deploy**. Enable **self-mutation**.
- **One-time manual step (document it):** the GitHub connection (`<<CODECONNECTIONS_ARN>>`) must be created
  once in the **CodePipeline → Settings → Connections** console and authorized with GitHub — CDK can't do the
  OAuth handshake. Everything else is code.
- Add a **manual approval** before the stage as a safety gate (I can remove it later).

---

## 8. Deploy / teardown / verify (README.md you generate)

- **Bootstrap once:** `cdk bootstrap`.
- **First deploy of the pipeline itself:** `cdk deploy PipelineStack` (after that, git push drives it).
- **Deploy everything locally without the pipeline (for fast iteration):** `cdk deploy --all`.
- **Teardown:** `cdk destroy --all` — because everything is `RemovalPolicy.DESTROY` + `autoDeleteObjects`,
  this must leave **zero** billable resources. Also delete the pipeline's S3 artifact bucket and any log
  groups. Print a final checklist of things to eyeball in Billing.
- Include per-layer **verify commands** (the `nc`, curl-with-SigV4, Kafka producer/consumer, GWLB traffic).

## 9. Diagrams (diagrams/ and diagram-site/)

- Add a `cdk-dia` (or `npx cdk-dia`) step that renders the synthesized app to `diagrams/architecture.png`.
- Document how to open `cdk.out/*.template.json` in **AWS Infrastructure Composer** (Console → Infrastructure
  Composer → menu → open template) to get an **AWS-generated** canvas of the deployed resources.
- These two are auto-generated and useful for debugging/completeness checks, but the **presentation-quality**
  diagram is the hand-built one in `diagram-site/index.html`, deployed by `diagram_stack.py` (§6) and served
  over CloudFront — that's the one to share/screenshot for the interview.

## 10. Guardrails / acceptance criteria

- Run **cdk-nag** (AwsSolutionsChecks); document any suppressions.
- Every stack `cdk synth`s clean before you propose deploying it.
- `cdk destroy --all` is verified to remove everything (lab hygiene — this matters).
- After the build, print a **feature checklist** confirming each Lattice feature in section 5 is present, with
  the file + line where it's implemented.
- The diagram at the `diagram_stack.py` CloudFront URL loads over HTTPS, is aligned/legible, and nowhere
  mentions "Palo Alto" — the firewall boxes show the real Linux software stack instead.

**Start now with the toolchain (§2) and `network_stack.py` (§4). After each stack: `cdk synth`, tell me what
you built, how to verify it, and wait for my go-ahead before deploying.**

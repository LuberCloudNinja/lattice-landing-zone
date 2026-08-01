import Image from "next/image";
import Link from "next/link";
import SiteFrame from "@/components/SiteFrame";
import HealthPill from "@/components/HealthPill";
import GithubLink from "@/components/GithubLink";
import ChatConsole from "@/components/ChatConsole";

const GITHUB_URL = "https://github.com/LuberCloudNinja/lattice-landing-zone";
const DIAGRAM_URL = "https://lubercloudninja.github.io/lattice-landing-zone/";

export default function HomePage() {
  return (
    <SiteFrame
      eyebrow="Cloud Architecture Portfolio"
      title="Luber J Guilarte Hay"
      subtitle="Sr Cloud Infrastructure and Application Architect, transitioning into Agentic AI and Generative AI. This site and everything behind it, the network, the pipeline, the chat assistant below, is one of my own projects, not a template."
    >
      <div className="grid gap-6 md:grid-cols-[220px_1fr] items-start mb-8">
        <div className="flex flex-col items-center gap-3">
          <Image
            src="/images/author-headshot.jpg"
            alt="Luber J Guilarte Hay"
            width={200}
            height={200}
            className="rounded-2xl object-cover w-full h-auto border border-white/20 shadow-[0_18px_50px_rgba(2,6,23,0.55)]"
          />
          <div className="text-center">
            <p className="font-semibold text-twin-text">Luber J Guilarte Hay</p>
            <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-[#9fc2ff] mt-1">
              Sr Cloud Infrastructure &amp; Application Architect
            </p>
            <p className="text-xs text-twin-muted mt-1">
              Transitioning into Agentic AI and Generative AI
            </p>
          </div>
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Link
            href="/blog/hybrid-cloud-airport-story/"
            className="rounded-2xl border border-white/15 bg-[#020a1f99] p-4 backdrop-blur-xl hover:border-[#5b9dff60] transition"
          >
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[#9fc2ff]">Read</p>
            <p className="mt-2 text-sm leading-relaxed text-[#d9e5ff]">
              The full story, plain English and deep technical detail, section by section, with the real diagrams for every layer.
            </p>
          </Link>
          <a
            href={DIAGRAM_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-2xl border border-white/15 bg-[#020a1f99] p-4 backdrop-blur-xl hover:border-[#5b9dff60] transition"
          >
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[#9fc2ff]">Explore</p>
            <p className="mt-2 text-sm leading-relaxed text-[#d9e5ff]">
              The live, interactive architecture reference on GitHub Pages, every AWS service depicted with the real icon set.
            </p>
          </a>
          <a href="#assistant-console" className="rounded-2xl border border-white/15 bg-[#020a1f99] p-4 backdrop-blur-xl hover:border-[#5b9dff60] transition">
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[#9fc2ff]">Ask</p>
            <p className="mt-2 text-sm leading-relaxed text-[#d9e5ff]">
              An assistant grounded in this project&apos;s own documentation, running on Amazon Bedrock.
            </p>
          </a>
        </div>
      </div>

      <div className="rounded-2xl border border-white/20 bg-white/[0.05] overflow-hidden mb-8">
        <Image
          src="/images/map-overview.svg"
          alt="Airport analogy map of the hybrid landing zone architecture"
          width={1200}
          height={640}
          className="w-full h-auto bg-white"
        />
        <div className="p-6">
          <h2 className="text-xl font-bold text-twin-text mb-2">Hybrid VPC Lattice Landing Zone</h2>
          <p className="text-sm leading-relaxed text-[#c9d6f2] mb-4">
            A multi region AWS reference architecture. Mandatory centralized traffic inspection across a
            Transit Gateway hub, a zero trust VPC Lattice service mesh running three routing primitives
            behind one IAM auth model, PrivateLink for one way service exposure, a permissions boundary
            enforced as a hard ceiling, a self mutating CDK pipeline, an incremental AWS Cloud WAN migration
            path, and an Agentic AI and SageMaker layer with real, governed, read mostly tool access. Fully
            defined in AWS CDK, Python, deployed through CI/CD, nothing clicked together by hand.
          </p>
          <div className="flex flex-wrap gap-2 mb-5">
            {[
              "AWS CDK",
              "Transit Gateway",
              "VPC Lattice",
              "Cloud WAN",
              "PrivateLink",
              "Gateway Load Balancer",
              "Bedrock AgentCore",
              "SageMaker",
              "Fargate",
              "DynamoDB",
              "S3 Vectors",
            ].map((tag) => (
              <span
                key={tag}
                className="font-mono text-xs rounded-full border border-white/15 px-3 py-1 text-twin-muted"
              >
                {tag}
              </span>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-4">
            <HealthPill />
            <GithubLink
              href={GITHUB_URL}
              className="font-mono text-xs text-[#9fc2ff] no-underline hover:underline"
            >
              View source on GitHub
            </GithubLink>
          </div>
        </div>
      </div>

      <section id="assistant-console" className="rounded-2xl border border-white/20 bg-white/[0.05] p-4 backdrop-blur-2xl scroll-mt-24">
        <p className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-[#b8d1ff]">Assistant Console</p>
        <ChatConsole />
      </section>
    </SiteFrame>
  );
}

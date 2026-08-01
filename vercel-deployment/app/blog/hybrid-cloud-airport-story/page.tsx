import type { Metadata } from "next";
import Image from "next/image";
import SiteFrame from "@/components/SiteFrame";
import AnalyticsBeacon from "@/components/AnalyticsBeacon";
import GithubLink from "@/components/GithubLink";
import { blogSections } from "@/content/blogSections";

export const metadata: Metadata = {
  title: "The Hybrid Cloud Airport Story, and the Technical Architecture Behind It",
  description:
    "A full walkthrough of a real hybrid AWS landing zone, told two ways at once: a plain English airport story, and a deep technical breakdown of every stack, from Cloud WAN to VPC Lattice to Agentic AI.",
};

const GITHUB_URL = "https://github.com/LuberCloudNinja/lattice-landing-zone";
const DIAGRAM_URL = "https://lubercloudninja.github.io/lattice-landing-zone/";

export default function BlogPost() {
  return (
    <SiteFrame
      eyebrow="AWS VPC Lattice, AWS Cloud WAN, Transit Gateway, Direct Connect and VPN, and Agentic AI with Amazon Bedrock, MCP and AgentCore"
      title="The Hybrid Cloud Airport Story"
      subtitle="Every section below is told twice. First in plain English, using an airport as the guide. Then in full technical depth, the way I would explain it to another engineer, with the real diagram for that layer. Read either track on its own, or both."
    >
      <AnalyticsBeacon articleSelector="article" />

      <div className="flex items-center gap-4 mb-8 pb-6 border-b border-white/10">
        <Image
          src="/images/author-headshot.jpg"
          alt="Luber J Guilarte Hay"
          width={64}
          height={64}
          className="rounded-full object-cover w-16 h-16 border border-white/20"
        />
        <div>
          <p className="font-semibold text-twin-text">Luber J Guilarte Hay</p>
          <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-[#9fc2ff]">
            Sr Cloud Infrastructure &amp; Application Architect
          </p>
          <p className="text-xs text-twin-muted">Transitioning into Agentic AI and Generative AI</p>
        </div>
      </div>

      <div className="rounded-2xl border border-white/15 bg-white/[0.04] p-5 mb-10">
        <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-[#9fc2ff] mb-3">On This Page</p>
        <div className="grid gap-1.5 sm:grid-cols-2">
          {blogSections.map((s, i) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              className="text-sm text-twin-muted hover:text-[#d9e5ff] transition flex gap-2"
            >
              <span className="font-mono text-[#5b9dff] w-6 flex-none">{String(i + 1).padStart(2, "0")}</span>
              {s.title}
            </a>
          ))}
        </div>
      </div>

      <article className="space-y-4">
        <p className="text-[17px] leading-[1.75] text-[#d9e5ff]">
          Picture an airport. Not the part passengers see, the part that makes it work: the control tower
          that decides who taxis where, the security checkpoint every single person passes through no
          matter which gate they are headed to, the private walkways that let a ground crew get from one
          terminal to another without walking through the public concourse, and the construction crew
          quietly renovating a wing at three in the morning without ever closing a runway.
        </p>
        <p className="text-[17px] leading-[1.75] text-[#d9e5ff]">
          That is what a cloud network actually is. Nobody who uses an app ever sees it, but it is the
          reason the app is fast, does not fall over, and does not leak data to a stranger. I built one of
          these, a real one, in AWS, using nothing but code, AWS CDK, Python, and this post walks through it
          twice. Once in plain language, using the airport as a guide. Once in the full technical depth I
          would use explaining it to another engineer, with the real diagram for that layer.
        </p>
        <p className="text-[17px] leading-[1.75] text-[#d9e5ff]">
          Every diagram below, both the simple airport illustrations and the technical AWS diagrams, was
          generated from this project&apos;s own source code. Nothing here is a stock image or a
          description of something that does not exist. You can read the code yourself at{" "}
          <GithubLink href={GITHUB_URL} className="text-[#9fc2ff] no-underline hover:underline">
            github.com/LuberCloudNinja/lattice-landing-zone
          </GithubLink>
          , including the full technical diagram source at{" "}
          <a
            href={DIAGRAM_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[#9fc2ff] no-underline hover:underline"
          >
            lubercloudninja.github.io/lattice-landing-zone
          </a>
          .
        </p>

        <div className="rounded-xl overflow-hidden border border-white/15 bg-white mt-8">
          <Image
            src="/images/map-overview.svg"
            alt="Airport map of the whole architecture"
            width={1200}
            height={640}
            className="w-full h-auto"
          />
        </div>

        {blogSections.map((s, i) => (
          <section key={s.id} id={s.id} className="pt-16 scroll-mt-24">
            <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-[#9fc2ff] mb-2">
              {String(i + 1).padStart(2, "0")} / {s.eyebrow}
            </p>
            <h2 className="text-2xl sm:text-3xl font-bold text-twin-text mb-6">{s.title}</h2>

            {s.illustration && (
              <div className="rounded-xl overflow-hidden border border-white/15 bg-white mb-6">
                <Image
                  src={`/images/${s.illustration}`}
                  alt={`${s.title} illustration`}
                  width={900}
                  height={420}
                  className="w-full h-auto"
                />
              </div>
            )}

            <div className="space-y-4 mb-8">
              {s.airport.map((p, j) => (
                <p key={j} className="text-[17px] leading-[1.75] text-[#d9e5ff]">
                  {p}
                </p>
              ))}
            </div>

            {s.technical.length > 0 && (
              <div className="rounded-2xl border border-[#5b9dff40] bg-[#5b9dff0d] p-5 sm:p-7">
                <p className="font-mono text-[11px] uppercase tracking-[0.16em] text-[#9fc2ff] mb-1">
                  Technical Deep Dive
                </p>
                <h3 className="text-lg font-semibold text-twin-text mb-4">{s.technicalTitle}</h3>

                {s.diagram && (
                  <div className="rounded-xl overflow-hidden border border-white/15 bg-[#0b1220] mb-5">
                    <div className="overflow-x-auto">
                      <Image
                        src={`/diagrams/${s.diagram}`}
                        alt={`${s.title} technical diagram`}
                        width={1600}
                        height={500}
                        className="min-w-[900px] w-full h-auto"
                      />
                    </div>
                  </div>
                )}

                <div className="space-y-4">
                  {s.technical.map((p, j) => (
                    <p key={j} className="text-[15px] leading-[1.75] text-[#c9d6f2]">
                      {p}
                    </p>
                  ))}
                </div>
              </div>
            )}
          </section>
        ))}

        <section className="pt-16 border-t border-white/10">
          <h2 className="text-2xl font-bold text-twin-text mb-4">Why I Built This</h2>
          <p className="text-[17px] leading-[1.75] text-[#d9e5ff] mb-4">
            Anyone can follow a tutorial and stand up a VPC with a public subnet. This is what I would
            actually build if a client handed me a blank AWS account and said we need a hybrid network that
            is genuinely secure, genuinely observable, and genuinely automatable, in code, not clicked
            together by hand.
          </p>
          <ul className="space-y-3 text-[17px] leading-[1.7] text-[#d9e5ff] mb-6">
            <li>
              <strong className="text-twin-text">Mandatory inspection, not optional.</strong> The routing
              tables do not offer a path around the checkpoint.
            </li>
            <li>
              <strong className="text-twin-text">A hard permissions ceiling, not an allow list.</strong>{" "}
              Defense against the single most common real world incident, an over scoped IAM policy.
            </li>
            <li>
              <strong className="text-twin-text">A pipeline that protects itself first.</strong> The
              deployment process can never drift silently out of sync with the code defining it.
            </li>
            <li>
              <strong className="text-twin-text">AI with read access and almost no write access.</strong>{" "}
              The one tool that can act opens a pull request. That is responsible AI assisted
              infrastructure in practice, not in a slide deck.
            </li>
            <li>
              <strong className="text-twin-text">Honesty about the edges.</strong> The Cloud WAN limitation
              is documented in the code, not hidden.
            </li>
          </ul>

          <div className="rounded-xl border border-[#5b9dff40] bg-[#5b9dff0d] p-6">
            <p className="mb-3 text-[#d9e5ff]">The full source is public.</p>
            <GithubLink
              href={GITHUB_URL}
              className="font-mono text-[#9fc2ff] font-semibold hover:underline"
            >
              github.com/LuberCloudNinja/lattice-landing-zone
            </GithubLink>
            <p className="mt-4 mb-2 text-[#d9e5ff]">
              And the technical diagram source, built from the same code, with the official AWS
              icon set.
            </p>
            <a
              href={DIAGRAM_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-[#9fc2ff] font-semibold hover:underline"
            >
              lubercloudninja.github.io/lattice-landing-zone
            </a>
          </div>
        </section>

        <section className="pt-16 border-t border-white/10">
          <h2 className="text-xl font-bold text-twin-text mb-4">About the Author</h2>
          <div className="flex flex-col sm:flex-row gap-6 items-start">
            <Image
              src="/images/author-presenting.jpg"
              alt="Luber J Guilarte Hay presenting"
              width={200}
              height={200}
              className="rounded-xl object-cover w-40 h-40 flex-none border border-white/20"
            />
            <div>
              <p className="font-semibold text-twin-text">Luber J Guilarte Hay</p>
              <p className="font-mono text-[11px] uppercase tracking-[0.1em] text-[#9fc2ff] mb-3">
                Sr Cloud Infrastructure &amp; Application Architect, transitioning into Agentic AI and
                Generative AI
              </p>
              <p className="text-[15px] leading-relaxed text-[#c9d6f2]">
                I design and build cloud infrastructure end to end, networking, security, automation, and
                how AI fits safely into an operations team rather than around it. This project is a self
                directed deep dive built to demonstrate exactly that. If you are hiring, evaluating a
                contractor, or just curious how a hybrid AWS network like this actually gets put together, I
                am glad to walk through any part of it in more depth.
              </p>
            </div>
          </div>
        </section>

        <p className="pt-10 text-sm text-twin-muted italic">
          By Luber J Guilarte Hay. Every illustration and diagram in this post was generated from the
          project&apos;s own source code.
        </p>
      </article>
    </SiteFrame>
  );
}

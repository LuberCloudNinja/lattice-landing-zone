/** @type {import('next').NextConfig} */
const nextConfig = {
  // No static export here, unlike app/frontend-next -- this copy runs as a
  // real Next.js server on Vercel so /api/assistant can be a live Route
  // Handler calling OpenAI, not a build-time artifact. app/frontend-next
  // stays the static-export copy CDK's BucketDeployment ships to S3.
  images: { unoptimized: true },
  trailingSlash: true,
};

module.exports = nextConfig;

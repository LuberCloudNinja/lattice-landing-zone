/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export -- ThreeTierStack's web tier is a private S3 bucket
  // behind CloudFront (OAC), deployed via CDK's BucketDeployment against a
  // plain directory of files. `output: 'export'` keeps that exact deploy
  // model instead of requiring a Node SSR runtime (Lambda@Edge / Fargate).
  output: "export",
  images: { unoptimized: true }, // next/image's optimizer needs a server -- not available under static export
  trailingSlash: true, // so `/blog/x` resolves to `/blog/x/index.html` on S3, matching CloudFront's default_root_object behavior
};

module.exports = nextConfig;

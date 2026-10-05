/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Next writes its own AGENTS.md and CLAUDE.md on `next dev`; the repo's
  // CLAUDE.md is the one to follow.
  agentRules: false,
};

export default nextConfig;

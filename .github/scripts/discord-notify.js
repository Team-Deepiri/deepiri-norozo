const fs = require("fs");

const WEBHOOK_URL = process.env.DISCORD_WEBHOOK_URL;
const EVENT_NAME = process.env.GITHUB_EVENT_NAME;
const EVENT_PATH = process.env.GITHUB_EVENT_PATH;
const REPO_FALLBACK = process.env.GITHUB_REPOSITORY || "unknown-repo";
const ACTOR_FALLBACK = process.env.GITHUB_ACTOR || "unknown-actor";
const SERVER_URL = process.env.GITHUB_SERVER_URL || "https://github.com";
const RUN_ID = process.env.GITHUB_RUN_ID;

if (!WEBHOOK_URL) {
  console.error("Missing DISCORD_WEBHOOK_URL secret.");
  process.exit(1);
}

if (!EVENT_PATH || !fs.existsSync(EVENT_PATH)) {
  console.error("Missing or invalid GITHUB_EVENT_PATH.");
  process.exit(1);
}

function readEventPayload() {
  return JSON.parse(fs.readFileSync(EVENT_PATH, "utf8"));
}

function truncate(text, max = 180) {
  if (!text) return "";
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function normalizeRef(ref) {
  if (!ref) return "unknown";
  if (ref.startsWith("refs/heads/")) return ref.replace("refs/heads/", "");
  if (ref.startsWith("refs/tags/")) return ref.replace("refs/tags/", "");
  return ref;
}

function colorForEvent(eventName, action, extra = {}) {
  if (eventName === "push") return 0x2f855a;
  if (eventName === "pull_request" && action === "opened") return 0x2563eb;
  if (eventName === "pull_request" && action === "reopened") return 0x2563eb;
  if (eventName === "pull_request" && action === "closed" && extra.merged) return 0x7c3aed;
  if (eventName === "pull_request" && action === "closed") return 0x6b7280;
  if (eventName === "issues" && action === "opened") return 0x2563eb;
  if (eventName === "issues" && action === "labeled") return 0xb45309;
  if (eventName === "issues" && action === "closed") return 0x6b7280;
  if (eventName === "create") return 0x0f766e;
  if (eventName === "release") return 0xb45309;
  return 0x374151;
}

function baseEmbed({ title, description, url, color, fields }) {
  const embed = {
    title,
    description,
    color,
    fields,
    timestamp: new Date().toISOString()
  };

  if (url) {
    embed.url = url;
  }

  if (RUN_ID) {
    embed.footer = {
      text: `GitHub Actions • Run ${RUN_ID}`
    };
  }

  return embed;
}

function commonFields(common, eventValue, extraFields = []) {
  return [
    { name: "Repository", value: `\`${common.repo}\``, inline: true },
    { name: "Event", value: `\`${eventValue}\``, inline: true },
    { name: "Actor", value: `\`${common.actor}\``, inline: true },
    ...extraFields
  ];
}

function buildPushEmbed(payload, common) {
  const branch = normalizeRef(payload.ref);
  const commits = Array.isArray(payload.commits) ? payload.commits : [];

  const commitLines = commits.slice(0, 4).map((commit) => {
    const shortSha = (commit.id || "").slice(0, 7);
    const summary = truncate((commit.message || "").split("\n")[0], 72);
    const url = commit.url || common.link;
    return `[\`${shortSha}\`](${url}) ${summary}`;
  });

  const commitBlock = commitLines.length
    ? `\n\n${commitLines.join("\n")}${commits.length > 4 ? `\n…and ${commits.length - 4} more commit(s).` : ""}`
    : "";

  return baseEmbed({
    title: `Push to ${branch}`,
    description: `${commits.length} commit(s) pushed to \`${branch}\`.${commitBlock}`,
    url: payload.compare || payload.head_commit?.url || common.link,
    color: colorForEvent("push"),
    fields: commonFields(common, "push", [
      { name: "Branch", value: `\`${branch}\``, inline: true }
    ])
  });
}

function buildPullRequestEmbed(payload, common) {
  const pr = payload.pull_request || {};
  const merged = payload.action === "closed" && pr.merged;

  let title = "Pull request updated";
  let eventValue = `pull_request.${payload.action}`;

  if (payload.action === "opened") title = "Pull request opened";
  if (payload.action === "reopened") title = "Pull request reopened";
  if (payload.action === "closed" && !merged) title = "Pull request closed";
  if (merged) {
    title = "Pull request merged";
    eventValue = "pull_request.merged";
  }

  const branchInfo = [];
  if (pr.head?.ref) branchInfo.push(`Source: \`${pr.head.ref}\``);
  if (pr.base?.ref) branchInfo.push(`Target: \`${pr.base.ref}\``);

  return baseEmbed({
    title,
    description: `[#${pr.number || "?"}](${pr.html_url || common.link}) ${truncate(pr.title || "(no title)", 140)}${branchInfo.length ? `\n\n${branchInfo.join("\n")}` : ""}`,
    url: pr.html_url || common.link,
    color: colorForEvent("pull_request", payload.action, { merged }),
    fields: commonFields(common, eventValue)
  });
}

function buildIssuesEmbed(payload, common) {
  const issue = payload.issue || {};
  const labelName = payload.label?.name;

  let title = "Issue updated";
  if (payload.action === "opened") title = "Issue opened";
  if (payload.action === "closed") title = "Issue closed";
  if (payload.action === "labeled") title = "Issue labeled";

  const extraFields = [];
  if (labelName) {
    extraFields.push({ name: "Label", value: `\`${labelName}\``, inline: true });
  }

  return baseEmbed({
    title,
    description: `[#${issue.number || "?"}](${issue.html_url || common.link}) ${truncate(issue.title || "(no title)", 140)}`,
    url: issue.html_url || common.link,
    color: colorForEvent("issues", payload.action),
    fields: commonFields(
      common,
      labelName ? `issues.${payload.action}` : `issues.${payload.action}`,
      extraFields
    )
  });
}

function buildCreateEmbed(payload, common) {
  if (payload.ref_type !== "branch") {
    return null;
  }

  const branch = payload.ref || "unknown";

  return baseEmbed({
    title: "Branch created",
    description: `A new branch was created: \`${branch}\`.`,
    url: `${SERVER_URL}/${common.repo}/tree/${encodeURIComponent(branch)}`,
    color: colorForEvent("create"),
    fields: commonFields(common, "create.branch", [
      { name: "Branch", value: `\`${branch}\``, inline: true }
    ])
  });
}

function buildReleaseEmbed(payload, common) {
  const release = payload.release || {};
  const tag = release.tag_name || "unknown-tag";
  const releaseName = release.name ? truncate(release.name, 120) : `Release ${tag}`;

  return baseEmbed({
    title: "Release published",
    description: `[${releaseName}](${release.html_url || common.link})`,
    url: release.html_url || common.link,
    color: colorForEvent("release"),
    fields: commonFields(common, "release.published", [
      { name: "Tag", value: `\`${tag}\``, inline: true }
    ])
  });
}

function buildEmbed(eventName, payload, common) {
  switch (eventName) {
    case "push":
      return buildPushEmbed(payload, common);
    case "pull_request":
      return buildPullRequestEmbed(payload, common);
    case "issues":
      return buildIssuesEmbed(payload, common);
    case "create":
      return buildCreateEmbed(payload, common);
    case "release":
      return buildReleaseEmbed(payload, common);
    default:
      return null;
  }
}

async function sendToDiscord(webhookUrl, body) {
  const response = await fetch(webhookUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Discord webhook failed: ${response.status} ${text}`);
  }
}

async function main() {
  const payload = readEventPayload();
  const repo = payload.repository?.full_name || REPO_FALLBACK;
  const actor = payload.sender?.login || ACTOR_FALLBACK;
  const link = payload.repository?.html_url || `${SERVER_URL}/${repo}`;
  const common = { repo, actor, link };

  const embed = buildEmbed(EVENT_NAME, payload, common);

  if (!embed) {
    console.log(`No Discord message sent for event: ${EVENT_NAME}`);
    return;
  }

  const body = {
    username: "GitHub Activity",
    allowed_mentions: { parse: [] },
    embeds: [embed]
  };

  await sendToDiscord(WEBHOOK_URL, body);
  console.log("Discord notification sent.");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
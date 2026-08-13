---
name: glean
description: |
  Use Glean to search Upstart's internal knowledge base — Confluence pages, Jira tickets, Slack threads, Gmail,
  Google Drive files, GitHub repos, and Guru cards. Glean respects all existing permissions so results are always
  scoped to what the user can access. Auto-triggers when the user asks to search internal docs, find decisions, look up
  processes, draft documents informed by internal context, or research anything about Upstart's tools, teams, or
  history. Example phrases: "search Glean", "find our docs on X", "what does our policy say about Y", "look up X in
  Confluence", "find internal guidance on Z", "search for Jira tickets about", "draft a PRD using internal context".
---

# Glean

Use Glean to search and retrieve content from Upstart's internal knowledge base. Glean indexes Slack, Gmail, Google
Drive, Confluence, Jira, GitHub, and Guru — providing a single entry point to company knowledge with permission-aware
results.

## When to Use Glean

**Invoke automatically when:**

- User asks to search internal docs, policies, processes, or decisions
- User wants context from Confluence, Jira, Slack, or Google Drive without specifying the tool
- User is drafting a document and needs internal research (PRDs, design docs, retrospectives)
- User asks about company history, architecture decisions, or past incidents
- User wants to find who works on something or owns a system
- User asks "how do we do X at Upstart" or "what's our policy on Y"
- User wants combined search across multiple sources at once (e.g., "find the Jira ticket and related Slack discussion
  and the Confluence design doc")

**Slack:** Prefer the Slack MCP when the user knows a specific channel, person, or thread. Use Glean for broad topic
searches or when the channel/DM is unknown. Slack MCP also required for: writing to Slack, `is:saved` bookmarks, channel
membership, very recent messages (Glean has an indexing delay).

**Confluence & Jira:** Prefer the Atlassian MCP when you need to write, fetch a full page, or navigate by space key. Use
Glean for cross-source searches or when the space is unknown.

**GitHub:** Glean indexes repos, PRs, and code via `app:github`. Prefer native GitHub tools for write operations
(creating issues, reviewing PRs, blame/diff). Use Glean for broad research ("how did we implement X?", "find PRs
mentioning Y").

## Confirmed Upstart Connectors

Glean indexes content from:

- Confluence
- GitHub
- Gmail
- Google Drive (Docs, Sheets, Slides)
- Guru
- Jira
- Slack

## Pi MCP access

Use Pi's `mcp` proxy tool to discover and call Glean tools. Start with `mcp({ search: "Glean search", server: "glean" })`, inspect a match when needed, then call it with `mcp({ tool: "glean_search", args: {...} })`. After the metadata cache is populated, the configured read-only Glean tools may also appear directly as `glean_search`, `glean_chat`, `glean_read_document`, and `glean_employee_search`.

If authentication is required, ask the user to run `/mcp-auth glean`; do not copy credentials from Claude Code.

## Three Core Tools

### `search` — Use for finding documents

Use when the user knows roughly what they're looking for and wants results with metadata (source, author, date). Search
returns ranked results with snippets.

### `chat` — Use for analysis and synthesis

Use when the user needs an answer synthesized from multiple sources, wants explanations, or is asking a complex
question. Glean's AI reasons across indexed content to provide a response with citations.

**When to use search vs chat:**

- Keywords or known document → `search`
- "What is our policy on X?" or "Explain how Y works" → `chat`
- Finding a specific Jira ticket or Confluence page → `search`
- Summarizing or comparing multiple documents → `chat`

### `read_document` — Use to get full document content

Use to retrieve the complete body of a document found via search. Pass the document identifier from search results (not
a raw external URL). Always call `search` first to locate the document, then pass its ID or URL from the search result
to `read_document`.

## Search Query Best Practices

**End questions with `?` for better chat results:**

- `"What are our deployment procedures?"` — better than `"Show deployment procedures"`
- The question format helps Glean's AI provide more contextual responses

**Specify connectors for precision:**

- `"Find the design doc for the payment service app:confluence"` — better than `"find the payment service design doc"`
- Include the source name when you know where the content lives

**Use `app:` filters to scope results:**

| Filter           | Source               |
| ---------------- | -------------------- |
| `app:confluence` | Confluence pages     |
| `app:gdrive`     | Google Drive files   |
| `app:gmail`      | Gmail messages       |
| `app:slack`      | Slack messages       |
| `app:jira`       | Jira tickets         |
| `app:github`     | GitHub repos/PRs     |
| `app:guru`       | Guru knowledge cards |

**Use person and time filters** (support varies by connector — not all sources honor every filter):

- `owner:"Jane Smith"` — documents owned/created by Jane
- `from:"Jane Smith"` — documents updated or commented on by Jane
- `updated:past_week` or `updated:today` — recently changed content
- `after:YYYY-MM-DD` or `before:YYYY-MM-DD` — date ranges

**Always quote multi-word values:**

- `app:confluence label:"Engineering Playbooks"` ✓
- `account:"Acme Corp"` ✓
- `type:document app:confluence label:tutorial` ✓

**Use `type:` to filter by content kind:**

- `type:document`, `type:page`, `type:presentation`, `type:email`

**Use quotation marks to emphasize keywords:**

- `"quarterly planning" app:gdrive updated:past_month`

## Use Cases by Role

### Product Management

- Draft PRDs informed by past feature discussions and internal research
- Roadmap tracking: `"What features are planned for X next quarter?"`
- Feature request analysis: `"Summarize Jira tickets and Slack threads about loan refinancing"`
- Performance reviews and brag documents: find PRs, Jira tickets, and Slack contributions
- Competitive analysis, design doc lookup, stakeholder research

### Engineering

- Debug errors: `"Search Glean for similar error messages in past incidents"`
- Code history: `"Why was this API endpoint implemented this way?"`
- Onboarding: `"Show architecture diagrams for the payment service app:confluence"`
- Code review standards, deployment runbooks, style guides

### Data / Analytics

- Trend analysis across customer support tickets, incident reports, and dashboards
- Combine Jira metrics with Slack context for comprehensive analysis
- Find Mode dashboards, Databricks notebooks, and related documentation

### All Roles

- `"How do I request access to X?"` — IT processes and Guru how-to guides
- `"Who owns the Y service?"` — team ownership and contacts
- `"What happened in the outage last month?"` — incident postmortems

## Read-only operations

Glean search, chat, document reads, and employee search are read-only. Pi exposes only those tools from this server.

## Troubleshooting

- **No results**: check permissions, try adding `app:` filter or more specific terms
- **Auth errors**: run `/mcp-auth glean`; contact IT if Glean access is missing
- **Server not loading**: run `/mcp` or `/mcp reconnect glean`, then `/reload` if direct tools changed

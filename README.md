# Personal Newspaper

A personal, non-commercial daily newspaper built from discussions in selected Reddit communities.

**Status:** Planning stage. This repository currently contains project documentation only. The application is not implemented or deployed, and Reddit Data API access is pending approval.

## Purpose

Help me catch up on relevant subreddit discussions through a concise daily digest.

There will be no advertising, subscriptions, sponsorships, or selling Reddit data.

## Planned Workflow

1. Retrieve public posts from a manually configured list of subreddits for the previous calendar day in a configured timezone.
2. Use an AI selection pass to choose approximately ten distinct story topics and identify their supporting posts.
3. Retrieve a limited sample of comments associated with the selected posts.
4. Run a separate AI writing request for each topic to produce a headline and a direct, three-to-five-sentence story.
5. Validate the generated output and source references before publishing a dated edition on the website.

## Reddit API Use

The planned API client is a server-side Python/Flask service hosted on Railway. Collection will use the official Reddit Data API, subject to approval and applicable terms.

- Read public posts and a bounded sample of public comments from selected subreddits.
- Apply configurable per-subreddit post limits and per-post comment limits.
- Do not access private communities or private messages.
- Do not post, comment, vote, or message Reddit users.
- Link published stories to the original source threads.
- Keep API credentials server-side in environment variables.

Selected post and comment text will be sent to external AI providers through OpenRouter for summarization. The application will not train or fine-tune models on Reddit content. Provider data-handling settings and applicable Reddit requirements will be reviewed before collection is enabled.

## Planned Configuration

Configuration will be managed through editable files rather than a public administration interface.

| File | Purpose |
| --- | --- |
| `config/sources.json` | Reddit collection method, subreddits, enabled status, post limits, and comment limits |
| `config/newspaper.json` | Timezone, target story count, and pipeline limits |
| `prompts/decider.md` | Selection model settings and instructions for choosing story topics and supporting posts |
| `prompts/writer.md` | Writing model settings, editorial instructions, tone, and story length |

These files are planned and have not yet been created.

## Planned Stack

| Area | Technology |
| --- | --- |
| Frontend | React, TypeScript, Vite, TanStack Router, Tailwind CSS, shadcn/ui |
| Backend | Python and Flask |
| Database | PostgreSQL |
| AI access | OpenRouter |
| Hosting and daily scheduling | Railway |

The public website will provide read-only access to published editions. Collection, AI requests, and publishing will run server-side, not through publicly accessible generation controls.

## Development

There is no runnable application yet. Setup instructions, tests, and deployment documentation will be added as implementation progresses.

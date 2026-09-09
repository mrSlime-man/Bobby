# LinkedIn Messages Manager

> Don't have time to manage your inbox, then this feature is for you!

If you receive a large volume of LinkedIn messages and do not have the time to manually review every conversation, this feature is designed for exactly that situation.

It helps busy users triage recruiter outreach, personal messages, spam, and inbound job-seeking messages faster by scanning the inbox, classifying conversations, and recording or executing actions based on your settings.

## What This Feature Does

The LinkedIn Messages Manager is a separate workflow that opens your LinkedIn inbox, reviews conversations, classifies them with the configured LLM, and then decides what to do with each conversation based on your settings.

This feature is intentionally separate from the job application flow in `main.py` because inbox triage has a different goal:

- It is not applying for jobs.
- It is not searching the jobs feed.
- It is helping you process recruiter outreach, spam, personal messages, and inbound job-seeking messages.

In practical terms, it acts like an inbox assistant for LinkedIn messaging.

## Why It Deserves Separate Documentation

This feature is more nuanced than a simple "read message and reply" script.

It has to balance several competing concerns:

- avoid replying to conversations where you already responded
- avoid sending awkward or incorrect replies to personal conversations
- detect recruiter/job messages and optionally treat them differently
- archive spam safely without confusing LinkedIn's menu actions
- support dry-run review before executing anything
- persist results after each processed conversation so you do not lose progress if the run is interrupted
- handle large and dynamic LinkedIn inbox lists where conversation cards can move, lazy-load, or become occluded while scrolling

Because of that, users need a clear mental model of what the tool does, what it will not do, and which settings control its behavior.

## Entry Point

Run the messages manager with:

```bash
uv run python linkedin_messages.py
```

This script is independent from the main job application runner.

## High-Level Workflow

When you run `linkedin_messages.py`, the workflow is:

1. Load message-manager settings from `config/linkedin_messages_manager_config.yaml`.
2. Load secrets from `.env`.
3. Load your resume text and structured resume.
4. Launch the browser and authenticate with LinkedIn.
5. Open the LinkedIn messaging inbox.
6. Optionally apply the `Unread` filter if `unread_only: true`.
7. Collect a bounded number of conversation summaries from the inbox list.
8. Process conversations one by one.
9. Persist each result to `data/output/linkedin/messages_dry_run.yaml` immediately after processing it.
10. Finish with a summary in logs and make the results available to the dashboard.

## What Happens For Each Conversation

For each conversation summary, the manager does not blindly open and reply. It follows a staged decision flow.

### Stage 1: Preview-Based Skip

Before opening the thread, the manager looks at the message preview snippet shown in the conversation list.

If the preview indicates that the latest visible preview starts with something like `You:` then the manager assumes you already replied and skips that conversation early.

This is important because it avoids unnecessary thread opens, LLM calls, and accidental over-processing.

### Stage 2: Open The Matching Conversation

If the preview does not show that you already replied, the manager opens the matching conversation by participant name.

The conversation list on LinkedIn is dynamic and can mark offscreen items as occluded. The manager is designed to handle that by scanning the full conversation list and scrolling items into view before clicking.

### Stage 3: Extract The Full Thread

Once the thread is open, the manager extracts:

- participant name
- participant headline
- conversation timestamp from the list summary
- preview snippet from the list summary
- individual thread messages
- inferred sender for each message

System cards and malformed message elements are skipped gracefully instead of blocking the run.

### Stage 4: Final "Already Replied" Guard

After extracting the thread, the manager checks whether the latest real message in the thread is from you.

If yes, it skips the conversation even if the list preview was ambiguous.

### Stage 5: LLM Classification

If the conversation still requires handling, the LLM classifies it into one of these categories:

- `personal_message`
- `job_offer_to_me`
- `looking_for_job`
- `marketing_spam`

The LLM also returns:

- confidence
- short reasoning
- proposed action

Supported proposed actions are:

- `skip`
- `keep`
- `draft_reply`
- `archive`
- `flag_spam_and_archive`

## Safety Rules Applied After Classification

The manager does not trust the LLM blindly.

There are normalization and safety rules applied after classification.

### Personal Message Draft Protection

If `skip_drafting_for_personal_messages: true`, then a conversation classified as `personal_message` will not be allowed to auto-draft a reply even if the LLM proposed `draft_reply`.

This guard exists to avoid obviously wrong responses in personal conversations, such as asking about a job opportunity that was never mentioned.

### Recruiter Automation Controls

If a conversation is classified as `job_offer_to_me`, the manager can optionally:

- star the conversation
- label the conversation as `Jobs`

These are controlled by config and can be enabled or disabled independently.

## Dry Run Versus Execute Mode

This is one of the most important concepts in the feature.

### Dry Run

When `dry_run: true`, the manager still reads messages, classifies them, drafts replies, and records what it would do, but it does not execute message actions.

That means:

- no replies are sent
- no conversations are archived
- no spam/archive action is executed
- no star action is clicked
- no label action is clicked

Dry run is the safest way to validate your settings and inspect results before turning on execution.

### Execute Mode

When execution is enabled, the manager may perform live actions depending on settings:

- send drafted replies
- archive conversations
- star recruiter/job-offer conversations
- label recruiter/job-offer conversations as `Jobs`

Execution is controlled by both `dry_run` and the action-specific toggles.

## Configuration File

The configuration file for this feature is:

`config/linkedin_messages_manager_config.yaml`

An example file is provided at:

`examples/config/linkedin_messages_manager_config.yaml`

## Configuration Reference

### Core Run Controls

`dry_run`

- If `true`, the manager records proposed actions but does not execute them.
- Recommended for the first few runs.

`execute_archives`

- If `true`, archive actions are allowed during execute mode.
- If `false`, archive proposals are only recorded.

`execute_replies`

- If `true`, drafted replies may actually be sent during execute mode.
- If `false`, reply proposals are recorded but not sent.

`max_conversations_to_scan`

- Maximum number of conversations to process in one run.
- The manager collects a small buffered number of summaries to compensate for skipped conversations, but it no longer over-scrolls aggressively.

`unread_only`

- If `true`, the script tries to click LinkedIn's `Unread` inbox filter before scanning conversations.

### Recruiter/Job Offer Automation

`auto_star_job_offers`

- If `true`, conversations classified as `job_offer_to_me` are starred.

`auto_label_job_offers`

- If `true`, conversations classified as `job_offer_to_me` are labeled as `Jobs`.

### Personal Message Protection

`skip_drafting_for_personal_messages`

- If `true`, personal conversations are prevented from auto-drafting replies.
- This is strongly recommended for most users.

### Reply Style Controls

`reply_tone`

- A short plain-language description of the tone the reply writer should follow.
- Example: `a thoughtful senior engineering leader`
- Example: `a concise founder`
- Example: `a warm and practical engineering manager`

`reply_max_characters`

- Character limit target for drafted replies.

`reply_short_paragraphs`

- If `true`, replies are encouraged to use short paragraphs separated by blank lines.

`reply_avoid_em_dash`

- If `true`, the LLM is instructed not to use em dashes.

### Old Message Reply Controls

`old_message_threshold_days`

- Number of days after which a message is considered old for apology-context purposes.

`old_message_apology_enabled`

- If `true`, the reply drafter adds context instructing the LLM to acknowledge a late response for older conversations.

`old_message_apology_reason`

- The explanation inserted into that apology context.
- Example: `you've been busy with multiple projects`
- Example: `you were heads down on client work`

`old_job_message_follow_up_enabled`

- If `true`, older recruiter/job-offer replies can include a polite follow-up about whether the role or opportunity is still open.

`old_job_message_follow_up_text`

- Plain-language instruction describing that follow-up.
- Example: `ask if the opportunity is still available`
- Example: `ask whether the role is still open`

## Recommended First-Time Setup

For a first run, use something conservative like this:

```yaml
dry_run: true
execute_archives: false
execute_replies: false
max_conversations_to_scan: 10
unread_only: true
auto_star_job_offers: true
auto_label_job_offers: true
skip_drafting_for_personal_messages: true
reply_tone: a thoughtful senior engineering leader
reply_max_characters: 600
reply_short_paragraphs: true
reply_avoid_em_dash: true
old_message_threshold_days: 60
old_message_apology_enabled: true
old_message_apology_reason: you've been busy with multiple projects
old_job_message_follow_up_enabled: true
old_job_message_follow_up_text: ask if the opportunity is still available
```

Then run:

```bash
uv run python linkedin_messages.py
```

Inspect the output file and dashboard before enabling live execution.

## Output Ledger

Results are persisted to:

`data/output/linkedin/messages_dry_run.yaml`

The filename is historical and is used for both dry-run and execute sessions.

This file is not overwritten from scratch at the end of the run. Instead, the manager upserts results continuously after each processed conversation.

That design is intentional for reliability.

If the run is interrupted because:

- you stop the process manually
- LinkedIn changes state mid-run
- the browser crashes
- the machine sleeps or disconnects

you still keep the latest processed conversation results in the ledger.

Each entry can include fields such as:

- `participant_name`
- `participant_headline`
- `timestamp`
- `snippet`
- `messages`
- `processing_status`
- `category`
- `confidence`
- `reasoning`
- `proposed_action`
- `draft_reply`
- `action_execution`
- `star_execution`
- `label_as_jobs_execution`
- `updated_at`
- `last_run_mode`

Some skipped conversations will naturally have fewer fields because the thread may not have been opened.

## Dashboard Integration

The messages manager is integrated with the local dashboard.

Relevant routes:

- main dashboard: `http://127.0.0.1:8000/`
- messages dashboard: `http://127.0.0.1:8000/messages`
- raw messages API: `http://127.0.0.1:8000/api/messages`

The messages dashboard is intentionally separate from the main page because the inbox triage data can become dense and deserves its own view.

## Important Behavioral Notes

### Skipped Conversations Do Not Consume The Processing Limit

If a conversation is skipped because:

- the preview shows you already replied
- the latest thread message is from you

it is still recorded in results, but it does not count against the final processed classification limit.

This matters because a run with `max_conversations_to_scan: 10` should not waste most of its budget on conversations that clearly require no action.

### The Summary Buffer Is Intentional

The manager may load slightly more conversation summaries than the final target count.

That buffer exists so that if some conversations are skipped early, the run can still process approximately the number of meaningful conversations you asked for.

The buffer is deliberately small and bounded so the script does not scroll deep into old inbox history unnecessarily.

### `flag_spam_and_archive` Archives Only

The name is historical, but the current safety behavior is conservative.

The manager does not try to click `Report / Block` flows. For spam handling it only archives the conversation.

This reduces the chance of incorrect destructive actions caused by UI ambiguity.

### LinkedIn UI Can Change

This feature depends on LinkedIn's inbox UI structure, including:

- conversation list item selectors
- thread action dropdowns
- send button selectors
- unread filter selectors

If LinkedIn changes these elements, the feature may need updates.

## Typical Usage Patterns

### Pattern 1: Review Only

Use dry-run mode to build a ledger of what the manager thinks each conversation is.

Good for:

- validating the classifier
- reviewing personal-message handling
- checking spam detection quality

### Pattern 2: Archive Spam But Do Not Reply

Useful if you trust archive decisions more than reply generation.

Example:

```yaml
dry_run: false
execute_archives: true
execute_replies: false
```

### Pattern 3: Fully Automated Recruiter Triage

Useful if you want recruiter conversations starred/labeled, spam archived, and strong reply suggestions or live replies.

This should only be used after several dry-run validation passes.

## Recommended Validation Process

Before relying on this feature in live mode:

1. Run with `dry_run: true`.
2. Review `data/output/linkedin/messages_dry_run.yaml`.
3. Review the dashboard messages page.
4. Confirm personal messages are being treated safely.
5. Confirm recruiter/job-offer conversations are categorized correctly.
6. Only then enable archive execution.
7. Only after that consider enabling reply execution.

## Troubleshooting

If the feature behaves unexpectedly, inspect:

- `logs/app.log`
- `logs/error_log.log`
- `data/output/linkedin/messages_dry_run.yaml`
- `data/debug/trace.zip` if tracing/debug artifacts were captured

Common things to verify:

- Is `unread_only` filtering the inbox more aggressively than expected?
- Is `skip_drafting_for_personal_messages` enabled?
- Is the reply style config too aggressive or too informal for your use case?
- Are old-message apology settings causing replies you do not want?
- Are you running in execute mode when you intended a dry run?

## Summary

The LinkedIn Messages Manager is best thought of as a configurable inbox triage system with optional automation, not just a reply bot.

Its safe use depends on understanding:

- dry run versus execute mode
- how classification works
- what safeguards override the LLM
- how the ledger is written continuously
- which settings define your preferred behavior

If you configure it carefully and validate with dry runs first, it can save significant time processing recruiter outreach and inbox noise while still keeping the user in control.

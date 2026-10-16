# BRD_Agent_V2: HeyPM meeting agent (Google ADK, Vertex AI)

The PM opens a meeting in HeyPM. The agent reads that meeting's transcript from GCS, writes the
**Summary**, **MOM** and **proposed JIRA actions** to Firestore, answers the PM's **questions** in the
chat, and on request **creates the JIRA items + emails the MOM** (after the PM confirms) and
**generates a BRD** from the PM's template (or the bundled default).

```
PM clicks a meeting ──► process_meeting(meeting_id)
                          GCS transcript ─► insight_extractor ─► ┬ summary_writer ─► summary
                                                                  ├ mom_writer     ─► MOM
                                                                  └ jira_planner   ─► JIRA plan
                          └─► Firestore meetings/{meeting_id}   (UI reads this)

PM asks in chat ──────► ask(meeting_id, question)     heypm_assistant (answers from transcript + record)
PM clicks Confirm ────► confirm_actions(meeting_id)   JIRA create/link/transition + MOM email
PM asks for a BRD ────► generate_brd(meeting_id, template_id?)  template ─► brd_writer ─► .docx/.md in GCS
```

## Firestore record (`meetings/{meeting_id}`)

Top-level fields are exactly the agreed schema (field names kept as specified):

| Field | Set by | Meaning |
|---|---|---|
| `meeting_id` | process | Document id = meeting id = transcript file name |
| `summary` | process | Markdown: one paragraph + "**Key points**" bullets (Summary panel) |
| `MOM` | process | Markdown MOM email body |
| `jira_actions_requirend` | process | `true` if the meeting needs any JIRA action |
| `jira_action_description` | process | Markdown list of proposed actions (A1, A2, …), each with why + timestamp |
| `bdr_generated` | generate_brd | `false` until the PM asks for a BRD and it has been created |
| `relevant_jira` | process, confirm | Existing keys mentioned in the call; created keys are added after confirm |
| `user_confirmation_provided` | confirm | `true` once the PM confirmed and JIRA ran without a blocking error |
| `last_modified_date` | every write | `YYYY-MM-DD` in `APP_TIMEZONE` (default Asia/Kolkata) |
| `last_modified_time_stamp` | every write | ISO 8601 with offset, e.g. `2026-10-16T09:51:02+05:30` |

Everything else the agent needs is kept in one nested map, **`agent_data`**, so the UI contract stays clean:
`status`, `transcript_uri`, `insights` (structured extraction), `jira_plan` (structured, executed on confirm),
`jira_results` (per action: status, key, error), `jira_status`, `mom_email` (sent/dry_run, recipients),
`brd` (`docx_uri`, `md_uri`, `template_name`, `generated_at`, `warnings`), `draft_warnings`, timestamps.

## For the API: call the service layer

```python
from BRD_Agent_V2.service import get_service      # one instance per process
svc = get_service()

await svc.process_meeting(meeting_id)              # -> public record (schema fields). ~30-90 s
svc.get_record(meeting_id)                         # -> public record, or None if not processed
svc.get_record(meeting_id, include_agent_data=True)

r = await svc.ask(meeting_id, "What did Anita say about SLAs?", user_id="pm@x", session_id=None)
#   -> {"session_id", "answer", "pending_confirmation"}; pass session_id back to continue the chat
#   If the PM asks the chat to create JIRA items, pending_confirmation = {"confirmation_id", "hint", "payload"}:
await svc.answer_confirmation(session_id, confirmation_id, confirmed=True, payload={"skip_refs": ["A3"]})

await svc.confirm_actions(meeting_id, skip_refs=["A3"], send_email=True)   # PM clicked Confirm
#   Idempotent: completed JIRA actions are not repeated; MOM is emailed once.

svc.list_templates()                                # [{"id","name","source","is_default"}], bundled default last
await svc.generate_brd(meeting_id, template_id=None) # None = PM's default template, else bundled default
```

Errors: `TranscriptNotFound`, `TemplateNotFound`, `MeetingNotProcessed`, `AlreadyConfirmed`
(re-processing a confirmed meeting needs `force=True`).

Suggested endpoints: `POST /meetings/{id}/process`, `GET /meetings/{id}`, `POST /meetings/{id}/chat`,
`POST /meetings/{id}/chat/confirm`, `POST /meetings/{id}/confirm`, `GET /templates`, `POST /meetings/{id}/brd`.
Processing takes up to a minute or two, so run it as a background task and let the UI poll `GET /meetings/{id}`
(or listen to the Firestore document).

Chat sessions: `ask()` keeps sessions in memory (fine for one instance). On Cloud Run with several instances,
build the chat `Runner` with a persistent session service (`DatabaseSessionService` on Cloud SQL, or
`VertexAiSessionService`); see `HeyPMService._chat`.

## Transcripts (GCS)

For a meeting id `MTG-123`, the agent looks for, in order:
`gs://TRANSCRIPT_BUCKET/TRANSCRIPT_PREFIX/MTG-123.txt|.vtt|.json|.md`, then any transcript file inside
`gs://…/TRANSCRIPT_PREFIX/MTG-123/`. A full `gs://` URI also works as the id.
Formats: plain text (Teams-style "Name   m:ss" blocks), **WebVTT** (Teams/Meet/Zoom exports), and **JSON**
(a list of `{speaker, start, text}` or `{"segments"|"utterances": [...]}`), all normalised to text.

## BRD templates (Templates tab)

Where they can live (`TEMPLATE_SOURCE=auto` checks all; the storage decision can be made later):

1. **Firestore** `brd_templates` collection: `{"name": "Finance BRD", "content": "<markdown>", "is_default": true}`
   or `{"name": …, "gcs_uri": "gs://…/finance.docx", "is_default": …}`
2. **GCS** folder `gs://TEMPLATE_BUCKET/templates/brd/`: `.docx`, `.md` or `.txt`; a file named `default.*` is the default
3. **Bundled default**: `templates/default_brd_template.md` (20 sections), always available

Word templates are converted to a Markdown skeleton (headings, guidance text, table headers). The writer keeps the
template's sections and order, fills placeholders, writes "TBC" for anything not discussed and lists it as an open
question, and flags the BRD if the template's last section is missing (usually a truncated generation).

## Run it locally

```powershell
cd C:\Users\nikhi\Desktop\NIKHIL\GoogleAI
pip install -r BRD_Agent_V2\requirements.txt
gcloud auth application-default login
cd BRD_Agent_V2
python run.py process MTG-2026-10-15-INVOICE-ALERTS
python run.py show MTG-2026-10-15-INVOICE-ALERTS
python run.py chat MTG-2026-10-15-INVOICE-ALERTS
python run.py confirm MTG-2026-10-15-INVOICE-ALERTS --skip A3,A4
python run.py brd MTG-2026-10-15-INVOICE-ALERTS
```

With `STORAGE_BACKEND=local` everything lives in `local_data/`: transcripts in `transcripts/`, templates in
`templates/`, records as JSON in `meetings/`, BRDs in `outputs/`. Switch to `STORAGE_BACKEND=gcp` and set the bucket
names to use GCS + Firestore.

**ADK dev UI:** from `GoogleAI`, run `adk web`, pick **BRD_Agent_V2**, and say "open meeting MTG-2026-10-15-INVOICE-ALERTS".

## GCP setup for `STORAGE_BACKEND=gcp`

```powershell
gcloud services enable firestore.googleapis.com storage.googleapis.com aiplatform.googleapis.com --project heypm-511616
gcloud firestore databases create --location=eur3 --project heypm-511616      # if none exists yet
gcloud storage buckets create gs://heypm-meetings --location=europe-west1 --project heypm-511616
gcloud storage cp local_data\transcripts\MTG-2026-10-15-INVOICE-ALERTS.txt gs://heypm-meetings/transcripts/
```

The runtime service account needs: Vertex AI User, Cloud Datastore User (Firestore), Storage Object Admin on the
bucket. Keep `JIRA_API_TOKEN` and `SMTP_PASSWORD` in Secret Manager in production.

## Safety

- `DRY_RUN=true` (default): confirm runs end to end, but nothing is emailed and JIRA is not touched (keys look like `FINAUTO-NEW1`).
- JIRA plan is typed and executed by code exactly as stored; the model never calls JIRA directly.
- Before writing to JIRA, existing keys in the plan must exist, or nothing is changed.
- Email recipients come only from `people.yaml`; unknown names are reported, never guessed.
- Long/degenerate model output is capped and cleaned; warnings are stored in `agent_data`.

## Tests

```powershell
cd BRD_Agent_V2
pip install pytest pytest-asyncio
pytest -q
```

Offline: scripted model, local stores, fake GCS and Firestore clients. Covers process → confirm → BRD, chat Q&A,
chat-initiated confirmation, template selection (.md and .docx), transcript formats.

## Layout

```
BRD_Agent_V2/
  agent.py            chat agent (heypm_assistant) + tools, ADK App "BRD_Agent_V2"
  service.py          HeyPMService: process / ask / confirm / templates / BRD   <- the API calls this
  pipeline.py         workflow agents: extractor, summary, MOM, JIRA planner, BRD writer
  prompts.py          instructions
  schemas.py          MeetingInsights, JiraPlan
  stores/             transcripts (GCS/local), meetings (Firestore/local), templates, files
  tools/              jira_ops, jira_client, email_ops, docx_render
  templates/          default_brd_template.md
  local_data/         local backend data (sample transcript included)
  people.yaml         name -> email / JIRA account
  run.py              CLI
  tests/
```

# ATLAS — AI Workplace Assistant

ATLAS is an autonomous AI agent for Galaxy Pharmaceuticals that answers employee questions in plain English by pulling live data from eight enterprise systems via Microsoft Teams.

---

## How It Works

```
User Message (Teams / Emulator / Postman)
        │
        ▼
  Bot Framework Adapter
        │
        ▼
    AtlasBot
        │
        ▼
  ToolOrchestrator ◄──────────────────────────────────────┐
        │                                                  │
        │  1. Send prompt + tool schemas → Claude          │
        ▼                                                  │
   Claude Sonnet 4.6                                       │
        │                                                  │
        ├── stop_reason: tool_use ──► Execute Tools ───────┘
        │                               ├── MasterControl (quality docs)
        │                               ├── SharePoint (files + URLs)
        │                               ├── Outlook (emails)
        │                               ├── Teams Chat (messages)
        │                               ├── NetSuite (ERP data)
        │                               ├── Oceaview (sensor readings)
        │                               ├── DataNinja (inventory)
        │                               └── Meeting Templates
        │
        └── stop_reason: end_turn ──► Final Answer → User
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| LLM | Claude Sonnet 4.6 (Anthropic) |
| Bot Channel | Microsoft Bot Framework v4 |
| Web Server | aiohttp + gunicorn |
| Auth | Microsoft Graph OAuth2 client credentials |
| Data Lake | Azure Blob Storage (read-only) |
| Hosting | Azure App Service |
| Language | Python 3.10+ |

---

## Prerequisites

- Python **3.10** or higher
- An Anthropic API key (`sk-ant-...`)
- *(Teams features only)* Azure AD app registration with Graph permissions:
  - `Sites.Read.All`
  - `Files.Read.All`
  - `Mail.Read`
  - `Chat.Read.All`
- *(Data tools only)* Azure Storage connection string

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/haristariq-oss/Atlas_bot.git
cd Atlas_bot
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Optional — defaults to INFO
LOG_LEVEL=INFO

# Required for Teams / SharePoint / Outlook / Teams Chat
BOT_APP_ID=
BOT_APP_PASSWORD=
BOT_TENANT_ID=

# Required for data tools (MasterControl, NetSuite, Oceaview, DataNinja)
AZURE_STORAGE_CONNECTION_STRING=
```

> Without `BOT_APP_*` credentials, SharePoint, Outlook, and Teams Chat tools are automatically skipped. The bot still runs with the remaining data tools.

---

## Testing Locally

### Step 1 — Verify your API key

```bash
python test_key.py
```

**Expected output:**

```
============================================================
ATLAS Hello World - SUCCESS
============================================================
I'm ATLAS, Galaxy Pharmaceuticals' AI workspace assistant...
============================================================
Model:     claude-sonnet-4-6
Tokens:    245 in, 38 out
Est. cost: $0.001305
============================================================
```

If this fails, your `ANTHROPIC_API_KEY` in `.env` is missing or invalid.

---

### Step 2 — Start the server

```bash
python bot_server.py
```

**Expected output:**

```
INFO | bot_server | ATLAS bot server starting on http://localhost:3978/api/messages
INFO | bot_server | Connect Bot Framework Emulator to that URL (leave App ID/Password blank)
INFO | bot_server | Health check: http://localhost:3978/health
```

---

### Step 3 — Choose a test method

#### Method A — Postman / curl (No extra tools required)

Send a `POST` to `http://localhost:3978/chat`:

```bash
curl -X POST http://localhost:3978/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What SOPs do we have about cleaning?",
    "conversation_id": "test-session-1",
    "user_email": "you@galaxypharma.net",
    "user_name": "Your Name"
  }'
```

**Response:**

```json
{
  "text": "Here are the cleaning SOPs from MasterControl...",
  "tool_calls": ["search_mastercontrol_documents"],
  "iterations": 2,
  "tokens_in": 1842,
  "tokens_out": 312,
  "cost_usd": 0.007266,
  "conversation_id": "test-session-1"
}
```

> `/chat` is **localhost-only**. It returns `403 Forbidden` from any non-loopback address.

---

#### Method B — Bot Framework Emulator (Full Teams experience)

1. Download [Bot Framework Emulator](https://github.com/microsoft/BotFramework-Emulator/releases)
2. Start the server: `python bot_server.py`
3. Open the Emulator → **Open Bot**
4. Enter Bot URL: `http://localhost:3978/api/messages`
5. Leave **App ID** and **App Password** blank
6. Click **Connect**

You can now chat with ATLAS exactly as you would in Microsoft Teams, including multi-turn conversations with full tool call traces visible in the Emulator sidebar.

---

### Health check

```bash
curl http://localhost:3978/health
```

```json
{ "status": "ok", "service": "atlas-bot" }
```

---

## Project Structure

```
atlas/
├── bot_server.py              # Entry point — aiohttp app + all routes
├── test_key.py                # Smoke test — verifies Anthropic API key
├── requirements.txt           # Python dependencies
├── startup.sh                 # Azure App Service startup command
├── .env.example               # Environment variable template
├── build_teams_package.py     # Generates Teams app manifest + zip
├── build_deploy_zip.py        # Generates Azure deployment zip
│
└── src/
    ├── config.py              # Config loader + fail-fast validation
    │
    ├── bot/
    │   └── atlas_bot.py       # Bot Framework ActivityHandler
    │
    ├── llm/
    │   ├── orchestrator.py    # Agentic loop — Claude + tools (max 5 iterations)
    │   ├── claude_client.py   # Anthropic SDK wrapper + cost tracking
    │   ├── conversation_memory.py  # Rolling window memory + Haiku compression
    │   ├── tools.py           # Tool definitions + factory (build_tools)
    │   └── sql_guard.py       # SELECT-only SQL validator
    │
    ├── data/
    │   ├── datalake_client.py # Azure Blob Storage read-only client
    │   ├── graph_client.py    # Microsoft Graph OAuth2 + HTTP helpers
    │   ├── user_resolver.py   # AAD object ID → email/UPN (LRU cached)
    │   ├── mastercontrol.py   # 645 quality docs — parallel load, in-memory
    │   ├── sharepoint.py      # SharePoint file search (Graph /search/query)
    │   ├── outlook.py         # Outlook mailbox search (Graph /users/{upn}/messages)
    │   ├── teams_chat.py      # Teams chat search (getAllMessages + keyword filter)
    │   ├── netsuite.py        # NetSuite ERP — customers, sales/purchase orders
    │   ├── oceaview.py        # Environmental sensors — humidity, pressure, temp
    │   ├── dataninja.py       # Parts + inventory (JSON Lines, in-memory cached)
    │   └── meeting_templates.py  # L10 / standup / retro / 1-on-1 structures
    │
    └── utils/
        ├── logger.py          # Structured logging setup
        └── console.py         # UTF-8 stdout fix (Windows compatibility)
```

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | Anthropic API key — must start with `sk-ant-` |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default: `INFO` |
| `BOT_APP_ID` | Teams only | Azure AD app registration client ID |
| `BOT_APP_PASSWORD` | Teams only | Azure AD client secret value (not the secret ID) |
| `BOT_TENANT_ID` | Teams only | Azure AD tenant ID |
| `AZURE_STORAGE_CONNECTION_STRING` | Data tools | Full Azure Storage connection string |

---

## Deploying to Azure App Service

### 1. Build the deployment package

```bash
python build_deploy_zip.py
# Output: atlas-deploy.zip
```

### 2. Deploy via Azure CLI

```bash
az webapp deploy \
  --resource-group <your-resource-group> \
  --name <your-app-name> \
  --src-path atlas-deploy.zip \
  --type zip
```

### 3. Set environment variables

```bash
az webapp config appsettings set \
  --name <your-app-name> \
  --resource-group <your-resource-group> \
  --settings \
    ANTHROPIC_API_KEY=sk-ant-... \
    BOT_APP_ID=... \
    BOT_APP_PASSWORD=... \
    BOT_TENANT_ID=... \
    AZURE_STORAGE_CONNECTION_STRING=...
```

### 4. Set the startup command

In Azure Portal → App Service → Configuration → General Settings → Startup Command:

```
gunicorn --bind=0.0.0.0:8000 --timeout 600 --worker-class aiohttp.GunicornWebWorker bot_server:app
```

---

## Deploying to Microsoft Teams

```bash
python build_teams_package.py
# Output: atlas-teams.zip
```

In Microsoft Teams:
1. Go to **Apps** → **Manage your apps**
2. Click **Upload a custom app**
3. Select `atlas-teams.zip`
4. Add ATLAS to a chat or channel

---

## Available Tools

| Tool | Data Source | What it answers |
|---|---|---|
| `search_mastercontrol_documents` | Azure Data Lake | SOP revisions, CAPA status, lifecycle, owner |
| `search_sharepoint` | Microsoft Graph | File content, URLs, SharePoint + OneDrive search |
| `search_outlook` | Microsoft Graph | Emails by keyword, sender, or date |
| `search_teams_messages` | Microsoft Graph | Teams chat messages scoped to asking user |
| `get_netsuite_data` | Azure Data Lake | Customers, sales orders, purchase orders |
| `query_environmental_sensors` | Azure Data Lake | Humidity, pressure, temperature readings |
| `query_dataninja_parts` | Azure Data Lake | Parts, inventory, manufacturing data |
| `prepare_meeting_agenda_template` | Static | L10 / standup / retro / 1-on-1 structures |

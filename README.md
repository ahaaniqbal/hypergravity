# Voice Hackathon

A Pipecat AI voice agent built with a cascade pipeline (STT → LLM → TTS).

## Configuration

- **Bot Type**: Telephony
- **Transport(s)**: Telnyx
- **Pipeline**: Cascade
  - **STT**: Deepgram
  - **LLM**: OpenAI
  - **TTS**: Cartesia

## Setup

### Setting Up Telnyx

#### 2. Purchase a Phone Number

If you haven't already, purchase a number from Telnyx:

- Log in to the [Telnyx Portal](https://portal.telnyx.com/)
- [Buy a number](https://portal.telnyx.com/#/numbers/buy-numbers)

#### 3. Create a TeXML Bin

A TeXML Bin contains the XML that tells Telnyx how to handle incoming calls.

1. Go to your [TeXML Bin configuration page](https://portal.telnyx.com/#/call-control/texml-bin)
2. Click **Create new TeXML Bin**
3. In the "Name" field, provide a name
4. Leave the "URL" field blank
5. In the "Content" field, add the TeXML:

    **For Local Development:**

    ```xml
    <?xml version="1.0" encoding="UTF-8"?>
    <Response>
      <Connect>
        <Stream url="wss://your-url.ngrok.io/ws" bidirectionalMode="rtp"></Stream>
      </Connect>
      <Pause length="40"/>
    </Response>
    ```

    Replace `your-url.ngrok.io` with your ngrok URL.

    **For Pipecat Cloud:**

    ```xml
    <?xml version="1.0" encoding="UTF-8"?>
    <Response>
      <Connect>
        <Stream url="wss://api.pipecat.daily.co/ws/telnyx?serviceHost=AGENT_NAME.ORGANIZATION_NAME" bidirectionalMode="rtp"></Stream>
      </Connect>
      <Pause length="40"/>
    </Response>
    ```

    Replace:
    - `AGENT_NAME` with the name of the agent you deployed to Pipecat Cloud
    - `ORGANIZATION_NAME` with the name of your Pipecat Cloud organization

6. Click **Save**

#### 3. Create a TeXML Application

1. Go to your [TeXML configuration page](https://portal.telnyx.com/#/call-control/texml)
2. Create a new TeXML app (if one doesn't exist already):
   - Add an application name
   - Under Webhooks, select **POST** as the "Voice Method"
   - Select **TeXML Bin URL** under Webhook URL Method
   - Select the TeXML Bin you created in the previous step
   - Click **Create** to save

   > Note: You'll see subsequent pages to set up SIP and Outbound - both are not required, so just skip them.

#### 4. Assign TeXML Application to Your Number

1. Navigate to [Manage Numbers](https://portal.telnyx.com/#/numbers/my-numbers)
2. Click the pencil icon next to your phone number
3. Select the TeXML application you just created
4. Click **Save**

### Server

1. **Navigate to server directory**:

   ```bash
   cd server
   ```

2. **Install dependencies**:

   ```bash
   uv sync
   ```

3. **Configure environment variables**:

   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

4. **Run the bot**:

   ```bash
   uv run bot.py
   ```

   The runner serves every transport; the caller selects which one (a web/mobile
   client picks its transport when it connects; a telephony provider connects to
   `/ws`).

   For telephony, expose the bot with a public tunnel and point your provider's
   webhook at it:

   ```bash
   ngrok http 7860
   # then set the provider's webhook to wss://<your-ngrok-host>/ws
   ```

## Project Structure

```
Voice Hackathon/
├── server/              # Python bot server
│   ├── bot.py           # Main bot implementation
│   ├── pyproject.toml   # Python dependencies
│   ├── .env.example     # Environment variables template
│   ├── .env             # Your API keys (git-ignored)
│   ├── Dockerfile       # Container image for Pipecat Cloud
│   └── pcc-deploy.toml  # Pipecat Cloud deployment config
├── .gitignore           # Git ignore patterns
└── README.md            # This file
```

## Deploying to Pipecat Cloud

This project is configured for deployment to Pipecat Cloud. You can learn how to deploy to Pipecat Cloud in the [Pipecat Quickstart Guide](https://docs.pipecat.ai/getting-started/quickstart#step-2-deploy-to-production).

Refer to the [Pipecat Cloud Documentation](https://docs.pipecat.ai/deployment/pipecat-cloud/introduction) to learn more about configuring, deploying, and managing your agents in Pipecat Cloud.

## Building with an AI coding agent

Extending this bot with Claude Code, Codex, or another AI coding assistant? Give it live, accurate Pipecat context instead of stale training data with the **Pipecat Context Hub** — a local index of Pipecat docs, examples, and API source your agent queries over MCP:

```bash
# Build the local index (first run takes a couple of minutes)
uvx pipecat-ai-context-hub@latest refresh

# Add it to your agent (use the line for the one you use)
claude mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve   # Claude Code
codex mcp add pipecat-context-hub -- uvx pipecat-ai-context-hub serve    # Codex
```

MCP servers load at session start, so add it before opening your coding session. See the [Pipecat Context Hub docs](https://docs.pipecat.ai/api-reference/context-hub) for the full setup.

## Learn More

- [Pipecat Documentation](https://docs.pipecat.ai/)
- [Pipecat GitHub](https://github.com/pipecat-ai/pipecat)
- [Pipecat Examples](https://github.com/pipecat-ai/pipecat-examples)
- [Discord Community](https://discord.gg/pipecat)
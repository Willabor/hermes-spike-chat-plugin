# hermes-spike-chat-plugin

A Hermes Agent **platform plugin** that connects an agent (e.g. **Queen**) to the
**spike-chat** box natively — so the gateway's **warm, persistent** session
receives spike-chat messages and replies back, instead of a cold `hermes -z`
one-shot per message (which re-pays ~58s prefill + re-registers MCP tools every
call, and times out / flakes under load). This is the durable replacement for
the throwaway relay.

## What it does
- Polls `GET {url}/api/feed?member={agent}` for new messages in the agent's
  channels + DMs.
- Triggers on human messages and `@{agent}` mentions (ignores self + other
  agents' chatter).
- Forwards each as a Hermes `MessageEvent` into the gateway's warm session.
- Posts the agent's reply back via `POST {url}/api/messages` as the agent
  (channel posts, or DMs when `project=="dm"`).

## Install (git-first)
On the Mac (Hermes host):
```bash
hermes plugins install Willabor/hermes-spike-chat-plugin
# or: hermes plugins install https://github.com/Willabor/hermes-spike-chat-plugin.git
```

## Enable (per-profile config.yaml, e.g. queen)
Under `agent.platforms`:
```yaml
spike_chat:
  enabled: true
  extra:
    url: http://100.67.169.29:4040
    member: Queen
    project: online-operations
    channel: order-ops
    role: order-ops
    poll_seconds: 3
```
Then restart the gateway: `hermes --profile queen gateway restart`.

## Notes
- `spike_chat` isn't in Hermes' built-in `Platform` enum, so the adapter passes
  the generic `Platform.RELAY` slot to the base (informational only).
- Telemetry footer (time · tools · count) is a planned follow-up — the base
  class runs the agent, so the footer hook lives in the message pipeline.
- Source of truth = this repo; pull + `hermes plugins update` to deploy changes.

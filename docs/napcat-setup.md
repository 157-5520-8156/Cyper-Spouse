# NapCat QQ Small-Account Adapter

NapCat is the QQ small-account route and an alternative to the official QQ bot
adapter. It happens to use OneBot v11 internally, but it is not the generic
OneBot route. Run only one QQ adapter while testing a conversation, otherwise
two accounts may reply to the same person.

The explicit choices are:

| `QQ_ADAPTER` | Route | Command |
| --- | --- | --- |
| `official` | QQ official bot | `scripts/run_qq_ws.sh` |
| `napcat` | QQ small account through NapCat | `scripts/run_napcat_adapter.sh` |
| `onebot` | Another OneBot v11 implementation | `scripts/run_onebot_adapter.sh` |

## Local topology

NapCat exposes its OneBot HTTP API on `127.0.0.1:3000`. It pushes inbound events
to the local Girl-Agent adapter on `127.0.0.1:8787/onebot/event`. Neither address
uses a public domain or leaves the machine.

1. In NapCat WebUI, add the `httpServers` and `httpClients` entries from
   `configs/napcat-onebot.example.json` to its OneBot configuration.
2. Replace both placeholder token values with the same long random value.
3. Put the same value in the project `.env` file:

   ```bash
   QQ_ADAPTER=napcat
   NAPCAT_API_URL=http://127.0.0.1:3000
   NAPCAT_ACCESS_TOKEN=replace-with-the-same-value
   NAPCAT_PROACTIVE_USER_ID=your-main-QQ-number
   ```

   `NAPCAT_PROACTIVE_USER_ID` is deliberately explicit: it is the QQ number that
   receives scheduled check-ins, life-event messages, and other background sends.
   It avoids confusing a normal QQ number with an official-bot OpenID when both
   routes have been tested on the same database.

4. Start the local adapter:

   ```bash
   scripts/run_napcat_adapter.sh
   ```

   The CLI now selects World v2 by default only when this is an unambiguous
   private-text deployment: `NAPCAT_ALLOW_GROUP_MESSAGES=false` and exactly
   one `NAPCAT_ALLOWED_PRIVATE_USER_IDS` entry.  In that mode groups,
   attachments, stickers, and a second private recipient are intentionally
   unsupported and are rejected by the V2 adapter; they never fall back into
   the archived Engine in the same process.

   Use `--archive-qq` (or `WORLD_V2_QQ_C2C_MODE=archive`) only to run the
   archived compatibility lane deliberately.  `WORLD_V2_QQ_C2C_MODE=v2`
   forces the selection but fails startup when the configuration is not the
   one-recipient private-text shape.  `WORLD_V2_QQ_C2C_ENABLED=true|false`
   remains a legacy explicit override while existing deployments are moved.

5. Verify NapCat and the adapter independently:

   ```bash
   curl --noproxy '*' -fsS http://127.0.0.1:3000/get_login_info
   curl --noproxy '*' -fsS http://127.0.0.1:8787/health
   ```

The adapter accepts NapCat's bearer token on inbound callbacks. It also accepts
the former `X-Signature` header for compatibility with prior OneBot setups.

For a non-NapCat OneBot implementation, set `QQ_ADAPTER=onebot`,
`ONEBOT_API_URL`, `ONEBOT_ACCESS_TOKEN`, and (for scheduled messages)
`ONEBOT_PROACTIVE_USER_ID`, then use `scripts/run_onebot_adapter.sh`.

## Routing with Antify

Create an Antify application rule for `/Applications/QQ.app` and set it to direct
connection. NapCat runs inside QQ, so this covers its QQ network traffic too.
Do not publish or proxy the loopback OneBot ports. `127.0.0.1` traffic is local.

## If QQ exits immediately

The OneBot adapter cannot start until QQ with NapCat stays open. First use the
NapCat installer/WebUI to repair or reinstall the QQ hook, then make sure NapCat
supports the installed QQ build. A TUN proxy rule cannot explain an immediate
application abort before NapCat opens its local port.

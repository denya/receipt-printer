# Alexa Session Monitor

This folder contains the Alexa custom-skill artifacts for the session-status
feature added to `receipt-printer`.

## Current endpoint

Working public endpoint:

- `https://claude-alexa-skill.denyamsk.com/alexa`

Skill ID:

- `amzn1.ask.skill.b7ed096b-ddbe-4894-809d-753eefed216b`

Important:

- The skill, interaction models, and endpoint were created successfully.
- The Alexa simulation APIs still report a certificate error against the
  Cloudflare edge wildcard certificate (`*.denyamsk.com`) even though the skill
  manifest is configured with `sslCertificateType: "Wildcard"`.
- Amazon's live device runtime may still behave differently from the simulator.
- The next practical verification step is a real-device test on one of the
  user's Alexas: `Alexa, open session monitor`.

## Skill shape

- Skill type: `Custom`
- Invocation name: `session monitor`
- Main intent: `GetSessionStatusIntent`
- Endpoint type: `HTTPS`
- Endpoint URL: `https://claude-alexa-skill.denyamsk.com/alexa`

## Console setup

In the Alexa developer console:

1. Create a new custom skill named `Session Monitor`.
2. Set the default language to `English (US)`.
3. On the interaction model page, import or paste
   [skill-package/interactionModels/custom/en-US.json](/Users/denya/code/random-vibe-coding/receipt-printer/alexa/skill-package/interactionModels/custom/en-US.json).
4. Optionally add the Spanish locale and import
   [skill-package/interactionModels/custom/es-ES.json](/Users/denya/code/random-vibe-coding/receipt-printer/alexa/skill-package/interactionModels/custom/es-ES.json).
5. On the endpoint page, set the HTTPS endpoint to
   `https://claude-alexa-skill.denyamsk.com/alexa`.
6. Copy the generated skill ID.
7. Set `ALEXA_SKILL_ID=<that skill id>` in
   `/home/denya/receipt-printer-service/.env` on the Pi.
8. Rebuild/restart the Pi service so app-id verification is enabled.

## Expected voice behavior

- `Alexa, open session monitor`
- `Alexa, ask session monitor for my active sessions`
- `Alexa, ask session monitor what is running`

The backend reads up to 3 active session summaries from the Pi SQLite cache.

## Verification note

The backend verifies Alexa request signatures and timestamps when
`ALEXA_VERIFY_SIGNATURE=1` and `ALEXA_VERIFY_TIMESTAMP=1`.

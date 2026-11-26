# Text-to-speech evaluation

- **Date:** 2026-10-26. Every licence and price below was read on the vendor's or project's own
  page that day. The sources are listed at the end with their URLs. Anything not confirmed
  there is marked **not verified**.
- **Scope:** this document only compares options. Amir 0.1 ships **no** TTS code or
  dependency, and never speaks news aloud. Greetings stay the owner's pre-recorded clips.
- **Where TTS would plug in:** playback already goes through the small `Player` port
  (`src/amir/voice/session.py`), and FFmpeg plays any file. A future TTS feature would render a
  line to a temporary file, then hand it to the same greeting queue. The voice-event monitor
  would not change.

## What Amir would actually use it for

News is never spoken, so the only plausible use is a short, dynamic line, such as greeting a
member by display name ("Good evening, Ярослава."). Fixed lines need no runtime TTS at all. The
owner can render them once, with any engine, and drop the files into `assets/greetings/`.

That use case shapes the criteria:

- Lines are short, about 40 characters.
- The input is a member's display name, which is personal data, in any script.
- One synthesis happens per arrival.
- It has to run on the server, on its CPUs alone.

**Illustrative volume.** This is an assumption, not a measurement. Take 50 greeted arrivals a
day at 40 characters each. That is about 2,000 characters a day, or about 60,000 a month.

## Options compared

Two local engines and three cloud services. ElevenLabs, Azure and Google are all included
because their price models differ.

| | Piper (local) | Kokoro-82M (local) | ElevenLabs (cloud) | Azure AI Speech (cloud) | Google Cloud TTS (cloud) |
|---|---|---|---|---|---|
| **Licence / terms** | Engine: GPL-3.0-or-later (`piper-tts` 1.8.0 on PyPI; the repository shows GPL-3.0). The original MIT `rhasspy/piper` was archived on 2025-10-06, and development moved to `OHF-Voice/piper1-gpl`. **Voices have their own licences** in each voice's `MODEL_CARD`. The voice list says "Piper is intended for personal use and text to speech research only" and "Some voices may have restrictive licenses". For example, `en_GB-alan-medium` says "License: See URL" and points to its Mycroft mimic3 dataset. | Weights: Apache-2.0 (Hugging Face model card). Library `kokoro` 0.9.4: Apache-2.0 (PyPI). G2P library `misaki`: Apache-2.0. Optional `espeak-ng` fallback: GPL-3.0. | Free plan: "does not include a commercial license and cannot be used for any commercial purpose", and published output must credit ElevenLabs. Paid plans include a commercial licence (Beta Services excluded). | Microsoft's commercial service terms; these were **not verified** for this document. | Google Cloud's service terms; these were **not verified** for this document. |
| **Operational cost** | US$0. Uses server CPU only. | US$0. Uses server CPU only. | Plans are priced in credits: Multilingual v2 costs 1 credit per character, and Flash/Turbo costs 0.5–1. Free gives 10k credits a month. Starter is US$6 a month for 30k credits, and Creator is US$22 a month for 121k credits (a promotion "Until Nov 1" lists their first month at US$1 and US$11). Pro is US$99 for 600k credits. At about 60k characters a month, Starter is too small, so this means Creator. | Free (F0) tier: "0.5 million characters free per month" (Neural). After that: US$15 per 1M characters for Neural and US$22 per 1M for Neural HD (retail, East US, Microsoft's price API). About 60k characters a month fits inside the free tier. | Free each month: Chirp 3: HD 1M characters, then US$30 per 1M. Neural2 1M, then US$16 per 1M. WaveNet and Standard 4M, then US$4 per 1M. Studio 1M, then US$160 per 1M. Billing must be enabled even for free usage. About 60k characters a month fits inside every free tier. |
| **Voice quality and character fit** | Widely used: its README lists Home Assistant and NVDA among its users. en_GB voices exist. **Not auditioned** for this document. | The model card claims "comparable quality to larger models while being significantly faster". British English voices exist (language code `b`). **Not auditioned.** | The plans list "Voice Design" and "Instant Voice Cloning". Cloning a real person needs their permission. **Not auditioned.** | Large neural voice catalogue, plus `MultilingualNeural` voices that speak several languages in one voice. | Chirp 3: HD comes in "30 distinct styles across many languages". |
| **Latency** | Local, no network. **Not measured** on the server. | Local, no network. **Not measured.** It is heavier than Piper (PyTorch). | Vendor claim for Flash v2.5: "~75ms", excluding application and network latency. A network round trip is added. | **Not verified.** A network round trip is added. | **Not verified.** A network round trip is added. |
| **Privacy** | Names never leave the server. | Names never leave the server. | Every display name is sent to ElevenLabs. | Every display name is sent to Microsoft. | Every display name is sent to Google. |
| **VPS CPU and memory** | PyPI publishes a `manylinux` wheel for the server's architecture, so it installs there as is. RAM use and speed are **not measured**. | The library depends on `torch` and `transformers`, a large addition to the image. The model is 82M parameters. RAM use and speed on the server are **not measured**. | None locally. | None locally. | None locally. |
| **Reliability and maintenance** | The original project is archived. The Open Home Foundation's new repository asks for maintainers. Each voice file has to be pinned and stored. | One maintainer. The last PyPI release was 0.9.4 on 2025-04-05. Model weights are pinned to v1.0 (2025-01-27). | Vendor-run. Needs an account, billing, an API key secret, and handling of outages and rate limits. | Vendor-run. Needs an Azure subscription and key. | Vendor-run. Needs a Google Cloud project, billing and credentials. |
| **Multilingual names** | Each voice speaks one language, with phonemes from espeak-ng. Voices exist for 39 languages, but a name in another script is read by the voice's own language rules. | v1.0: 8 languages and 54 voices, but each voice has one language. Japanese and Mandarin need extra packages. | Flash v2.5 supports 32 languages, and Multilingual v2 supports 29. | `MultilingualNeural` voices handle mixed-language text in one voice. The full language count is **not verified**. | The count is **not verified**. |

## Trade-offs in brief

- **Cloud services sound best but cut against two standing rules.** A standing rule allows no paid
  services or API keys beyond the Discord bot token. The privacy anti-goal also
  argues against sending every member's display name to a third party. Azure's free tier, or
  Google's, would cover Amir's volume at no cost. Even so, each still needs a billing account and
  an API secret, and each is a new outbound dependency that can fail.
- **Local engines keep names on the server and cost nothing.** What they cost instead is CPU,
  image size and maintenance.
- **Piper is light and runs on the server's CPU out of the box.** Its engine is GPL, though, and its voice
  licences vary and lean towards personal or research use. Each voice's licence would have to be
  read before use. GPL obligations would also matter if Amir's code were ever distributed with
  Piper linked in.
- **Kokoro has the cleanest licences.** Both the code and the weights are Apache-2.0, and it is
  the newer model. It is much heavier (PyTorch), and it has one maintainer.

## Recommendation

**Kokoro-82M, run locally on the server.** This applies only if a dynamic-speech feature (such as
greeting by name) is ever approved.

- **It meets the hard rules:** no paid service, no new API secret, and member names never leave
  the server.
- **Its licences are permissive and verified:** Apache-2.0 for the weights, the `kokoro` library
  and `misaki`. Keep the GPL `espeak-ng` fallback off unless a non-English name needs it.
- **It has British English voices,** which suit a composed butler.

Before building anything:

1. **Measure Kokoro on the server:** image size, RSS, and time to synthesise a 40-character
   line. If PyTorch makes it too heavy, fall back to **Piper**, using a British voice whose
   `MODEL_CARD` licence allows the use.
2. **Never persist** the synthesised audio or the text: it contains member names (the
   surveillance anti-goal). Render to `/tmp` and delete after playback.
3. **Keep TTS behind the existing `Player` port and greeting queue.** Then expiry (30 s),
   no-overlap and capacity rules apply unchanged, and a TTS failure must still never block the
   text announcement.

For **fixed** lines, no runtime TTS is needed at all. Render them once, offline, and add them as
clips.

## Sources (all accessed 2026-10-26)

- Piper engine: <https://github.com/OHF-Voice/piper1-gpl> (GPL-3.0; `pip install piper-tts`;
  "looking for maintainers"; lists Home Assistant and NVDA among the projects using Piper).
- Piper on PyPI: <https://pypi.org/project/piper-tts/> (1.8.0, released 2026-09-04;
  "GPL-3.0-or-later"; a `manylinux_2_17` wheel).
- Original Piper: <https://github.com/rhasspy/piper> ("archived by the owner on Oct 6, 2025";
  MIT; "Development has moved").
- Piper voices: <https://raw.githubusercontent.com/OHF-Voice/piper1-gpl/main/docs/VOICES.md>
  (per-voice `MODEL_CARD` licences; "intended for personal use and text to speech research
  only"); <https://huggingface.co/rhasspy/piper-voices/raw/main/en/en_GB/alan/medium/MODEL_CARD>
  ("License: See URL", dataset `MycroftAI/mimic3-voices`).
- Kokoro weights: <https://huggingface.co/hexgrad/Kokoro-82M> (Apache-2.0; 82M parameters; v1.0
  2025-01-27, 8 languages, 54 voices).
- Kokoro library: <https://pypi.org/project/kokoro/> (0.9.4, 2025-04-05, Apache);
  <https://github.com/hexgrad/kokoro> (Apache-2.0; British English `b`);
  <https://raw.githubusercontent.com/hexgrad/kokoro/main/pyproject.toml> (depends on `torch`,
  `transformers`, `misaki[en]`).
- misaki: <https://github.com/hexgrad/misaki> (Apache-2.0). espeak-ng:
  <https://github.com/espeak-ng/espeak-ng> (GPL-3.0).
- ElevenLabs prices: <https://elevenlabs.io/pricing> (plans, credits, "Until Nov 1" first-month
  promotion; credits per character by model; "Voice Design" and "Instant Voice Cloning" features).
- ElevenLabs free-plan terms:
  <https://elevenlabs.io/docs/help-center/legal/can-i-publish-the-content-i-generate-on-the-platform>
  ("The free plan does not include a commercial license and cannot be used for any commercial
  purpose"; attribution "elevenlabs.io" or "11.ai").
- ElevenLabs models: <https://elevenlabs.io/docs/overview/models> (Flash v2.5 "~75ms†", 32
  languages; Multilingual v2, 29 languages).
- Azure free tier: <https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/>
  ("0.5 million characters free per month"; the per-character prices on that page did not render).
- Azure per-character prices: Microsoft's retail price API,
  <https://prices.azure.com/api/retail/prices> filtered to `armRegionName eq 'eastus'` and
  `contains(meterName, 'Neural')`: "S1 Neural Text To Speech Characters" US$15 per 1M (effective
  2024-02-01); "Neural HD Text to Speech Characters" US$22 per 1M (effective 2026-03-01).
- Azure multilingual voices:
  <https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts>
  (`MultilingualNeural` voices; page updated 2026-10-22).
- Google prices: <https://cloud.google.com/text-to-speech/pricing> (per-voice-type free tiers and
  prices as quoted above).
- Google voices: <https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types>
  (Chirp 3: HD "30 distinct styles across many languages").

# Research: conversational AI behind social-autoposter

Curated reading list. The framing: a social-autoposter reply is not "posting an ad."
It is **proactively recommending an unfamiliar tool to a stranger mid-conversation,
helpfully enough that it reads as a tip, not spam.** That sits at the intersection of
four fields below. Each entry notes *why it maps* to what this repo actually does.

PDFs for the freely available (arXiv) papers are in `pdfs/`. Paywalled / non-arXiv
items are linked only.

---

## 1. Conversational recommendation (the core frame)

The academic name for the job: surface the right item through dialogue, not a one-shot ad.

- **Jannach, Manzoor, Cai, Chen — A Survey on Conversational Recommender Systems** (ACM CSUR 2021)
  - arXiv: https://arxiv.org/abs/2004.00646 · PDF: `pdfs/2004.00646_CRS-survey-Jannach.pdf`
  - Foundational taxonomy of CRS approaches, interaction modalities, evaluation. Read first.
- **Gao, Lei, He, Kan, Chua — Advances and Challenges in Conversational Recommender Systems: A Survey** (2021)
  - arXiv: https://arxiv.org/abs/2101.09459 · PDF: `pdfs/2101.09459_CRS-advances-challenges.pdf`
  - Complements Jannach with open problems (dialogue understanding/generation, exploration, eval).
- **He et al. — Large Language Models as Zero-Shot Conversational Recommenders** (CIKM 2023)
  - arXiv: https://arxiv.org/abs/2308.10053 · PDF: `pdfs/2308.10053_LLM-zeroshot-CRS.pdf`
  - Our recommender IS an LLM. Shows zero-shot LLMs beat fine-tuned CRS but have weak
    "collaborative knowledge" (they don't know what is actually popular) — the gap our
    engagement-stats / style-scoring layer fills.
- **Hou et al. — Large Language Models are Zero-Shot Rankers for Recommender Systems** (ECIR 2024)
  - Code/repo: https://github.com/RUCAIBox/LLMRank
  - LLM-as-ranker via instruction-following over interaction history + candidates.

## 2. Proactive dialogue (should I even reply, and how do I steer in?)

The picker's real decision: is this thread worth interjecting on, and how do you pivot
to the tool without being abrupt?

- **Deng et al. — A Survey on Proactive Dialogue Systems: Problems, Methods, and Prospects** (2023)
  - arXiv: https://arxiv.org/abs/2305.02750 · PDF: `pdfs/2305.02750_proactive-dialogue-survey.pdf`
- **Deng et al. — Towards Human-centered Proactive Conversational Agents** (SIGIR 2024)
  - arXiv: https://arxiv.org/abs/2404.12670 · PDF: `pdfs/2404.12670_human-centered-proactive.pdf`
  - Topic-shifting and initiative-taking, formalized.
- **Liu et al. — Proactive Conversational Agents with Inner Thoughts** (2025)
  - arXiv: https://arxiv.org/abs/2501.00383 · PDF: `pdfs/2501.00383_inner-thoughts.pdf`
  - Most on-the-nose paper here: models *whether and when* an agent should speak unprompted
    in a live conversation. That is exactly our "is this tweet worth a reply" gate.

## 3. Serendipity (the anti-spam framing)

Formal answer to "introduce a tool they weren't aware of but will value, without being
obvious or annoying."

- **Kotkov, Wang, Veijalainen — A Survey of Serendipity in Recommender Systems** (Knowledge-Based Systems 2016)
  - https://www.sciencedirect.com/science/article/abs/pii/S0950705116302763 (paywalled)
  - Defines serendipity = relevant + novel + unexpected; cure for "obvious, already-known"
    suggestions. That is the whole pitch of a good reply.
- **Deep Learning Models for Serendipity Recommendations: A Survey and New Perspectives** (ACM CSUR 2023)
  - https://dl.acm.org/doi/10.1145/3605145 (paywalled)
  - Modern methods for engineering surprise-that-lands.

## 4. Pedagogical / curiosity agents (help them learn something new)

Helping people learn things they weren't aware of, directly.

- **Dubois et al. — Conversational agents for fostering curiosity-driven learning in children** (2022)
  - arXiv: https://arxiv.org/abs/2204.03546 · PDF: `pdfs/2204.03546_curiosity-driven-learning.pdf`
  - Built around **knowledge-gap awareness**: make someone realize a gap they didn't know
    they had, then guide them to fill it. The persuasion mechanic behind a good tool-intro reply.
- **Dialogic Pedagogy for Large Language Models: Aligning Conversational AI with Proven Theories of Learning** (2025)
  - arXiv: https://arxiv.org/abs/2506.19484 · PDF: `pdfs/2506.19484_dialogic-pedagogy-llms.pdf`
- **Wang et al. — Persuasion for Good: Towards a Personalized Persuasive Dialogue System for Social Good** (ACL 2019)
  - arXiv: https://arxiv.org/abs/1906.06725 · PDF: `pdfs/1906.06725_persuasion-for-good.pdf`
  - 10 labeled persuasion strategies + personalization model + explicit ethics discussion.
    Maps onto our engagement-style A/B taxonomy (critic, storyteller, etc.) and the line
    between helpful and manipulative.

---

## Start here (3)

1. **Jannach CRS survey** (`2004.00646`) — the field.
2. **Inner Thoughts** (`2501.00383`) — when to speak.
3. **Serendipity survey** (Kotkov 2016) — introduce-the-unknown-without-spam.

Together they describe the product better than the "autoposter" framing does.

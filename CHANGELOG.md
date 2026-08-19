# Changelog

All notable changes to this project will be documented in this file.

## [v1.3.0] (inferred)

### Features

- Add pending_threads script to persist Reddit thread drafts for retry ([d4e0b42](https://github.com/m13v/social-autoposter/commit/d4e0b42799469e1b16878513691e3e2f45ca13c4))
- Add twitter_batch_phase.py for phase-aware salvage timing ([148775e](https://github.com/m13v/social-autoposter/commit/148775ec861ae2e65854fa05c13b296953675a21))
- Add batch phase tracking and exit cleanup to run-twitter-cycle.sh ([60fbb5f](https://github.com/m13v/social-autoposter/commit/60fbb5f42c077ae069db50a0cd00339c806597ad))
- Add retry loop with backoff for Claude API Usage Policy errors ([85f4f1a](https://github.com/m13v/social-autoposter/commit/85f4f1a099f8e8da862724c9f04aff79fa0277c7))
- Add twitter_batches table for phase-aware salvage ([64f6ff6](https://github.com/m13v/social-autoposter/commit/64f6ff6a811b33d0fa373c529f87933783d5dbe5))
- Add replied_in_sent metric to DM stats calculation ([526aa06](https://github.com/m13v/social-autoposter/commit/526aa066b8c71b25abfdd298e861511bdbca3ca8))

### Fixes

- Fix short link idempotency check to use post-UTM target URL ([eab08e1](https://github.com/m13v/social-autoposter/commit/eab08e1963a654f2a896c991170ac599d6f6f576))

### Chores

- Update posting prompt to spell out costs and improve readability ([bb7019b](https://github.com/m13v/social-autoposter/commit/bb7019b2d83b4f2dbdf1418d94f3c9ac3e995d6c))
- Update salvage logic to use phase-aware timeouts ([94ce891](https://github.com/m13v/social-autoposter/commit/94ce8915d51797598deca0904316672d92aff7d1))
- Refactor job history rendering to use stable keys and signatures ([88fea43](https://github.com/m13v/social-autoposter/commit/88fea437ac3d55595a686e768d9873f0a566d9cb))
- Update LinkedIn search queries from 6 to 8 per run ([c4bd94e](https://github.com/m13v/social-autoposter/commit/c4bd94e3fac734f9f39d4d8f28fc7a81ef858861))
- Update LinkedIn prompt to favor posting and define hard/soft criteria ([8e5e91c](https://github.com/m13v/social-autoposter/commit/8e5e91c86bcbcb7654acdc8ad5f958ce0084bd88))
- Update LinkedIn prompt to require exactly 6 search queries ([c08d85b](https://github.com/m13v/social-autoposter/commit/c08d85b1eb14e243ff7137f01ae59b212c632d80))
- Increase LinkedIn search query limit to 4-6 ([c85fee7](https://github.com/m13v/social-autoposter/commit/c85fee78eb6030ece41a099582e9f5ed81993094))
- Refactor URL punctuation stripping to use _TRAILING_PUNCT ([c57aa18](https://github.com/m13v/social-autoposter/commit/c57aa18ec439344e57ffaccb5aff9b4e5ed4862d))
- Update URL regex to match and normalize bare-domain URLs ([9b47538](https://github.com/m13v/social-autoposter/commit/9b47538a4b654edb6201d01f204496ab911b84a8))
- Update booking link wrapping and add LinkedIn pre-send wrap step ([5d7d98a](https://github.com/m13v/social-autoposter/commit/5d7d98a92915ee8380b1a27854017da3c388634e))
- Update engage-dm-replies to use automatic link wrapping ([dcb0d20](https://github.com/m13v/social-autoposter/commit/dcb0d20ade980d4ef4ab9c28f9b6ecf6b377ca91))
- Update DM link stats to query dm_links and support target_projects ([838fe0b](https://github.com/m13v/social-autoposter/commit/838fe0be4e07a6c0540830f4aa9b0d59e0574ddb))
- Replace regex-based DM link detection with dm_links_count ([a14e338](https://github.com/m13v/social-autoposter/commit/a14e338f707d669ada166a632f5418938862813c))

_Changelog updated with OpenHelper :)_

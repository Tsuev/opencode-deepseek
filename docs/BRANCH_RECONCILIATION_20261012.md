# browser-providers reconciliation — 2026-10-12

Compared branch `c2ab729` with main `aa8e3be` offline using Git history,
patch equivalence and the added/deleted payload of the unmatched patches.
The branch has 36 commits absent by ancestry: 35 ordinary commits and one merge.
This is a history divergence, not 36 missing fixes.

## Result

- 29 ordinary commits have patch-equivalent changes in main (`git cherry`).
- The five functional commits below have identical added/deleted payloads to
  their main counterparts. Different README context explains four changed patch
  IDs; the account-safety patch is identical (the checkout has shallow history).
- `cb687dd` only prepares documentation for upstream: changes clone/CI URLs from
  DmitrL-dev to Tsuev and replaces fork-specific introductory text. These changes
  are intentionally not imported into this fork. Current account warnings remain.
- `3fa17d9` merges main into the feature branch; it adds no separate feature patch.

| Feature commit | Main counterpart | Scope |
| --- | --- | --- |
| 327460c | baa48d4 | Stop replays and persist account refusals |
| f6d0fd3 | 8675897 | Harness setup and offline checks |
| f3d0ada | a0736b2 | Browserless model selection |
| 729b3ed | 86dc523 | Kimi Instant and guarded renewal |
| 73a3d6a | 960b511 | Kimi K3/K2.8 and capacity refusal |

No missing functional patch was found. Main has subsequent fixes and refactors;
replacing it with the old branch would discard those improvements. Keep the
branch while upstream PR #7 uses it. No history rewrite or branch deletion.

## Patch-equivalent branch commits

- `03b85e7004551413c9391ce6670f8c7e5260b7cd`
- `a9dd2bf34af1ba761c595cc755f3af5ebeb178c8`
- `da740e68105d5520060016783f6124b78701db25`
- `3637ca4c77c04b990d4a4cc55bc6c3cde429283b`
- `627b2142194aab713b7cf6366f48063c6fba98bc`
- `8984ccf6ac630e065f54d06c08c71382ce934334`
- `a3f68921b2ef97e72606d01250bc9bf084945c9b`
- `11ca898672a7d3cd2fb580297accc25915b9bdec`
- `14729926af47f288b8b02265c06153aafb40b66d`
- `f79fff9b64a08eeed33b928e7b97fe3556a18c15`
- `8918b50eb2851c038341d3cc01514553bfbd2ce9`
- `0f262b4e76f22a245b0952f50dafc8a5ad9f1bbb`
- `44573b76eb9f5c60ebbd5f08c718c7d6dc7d8923`
- `0fa849c87735e288f3b9fddd6a864339be28b6d1`
- `ec41d0cea391756a9ea6b8b710e2f9381eadb361`
- `d0c34a81a485e486fc3f178987662432cf26db51`
- `b50d782452b27a35dcba085302b22ccaac44b709`
- `0aeb2838c70d16160107631cbcbfdbb7c56b6853`
- `9f29e5781a626d821fca171f816fb2082568156d`
- `d1d5d5c2843024e329e4babf0ff6eb17991b6b13`
- `5d72aaba176eb9e38feebf569109b4d830add95b`
- `457286b57b3c63365e5728046a2908e4c2a6674d`
- `c2d1c2bb80f25bd93bddfe7c2914691d6c960c16`
- `62ed7da874c6bf926647e4d2ccdcb5225e974531`
- `b1af0e1206317d33a9308340a8104aeb66742b29`
- `de0650d0cd00f5a1672cf534daaa6f82f8bb0144`
- `2463d97b355b2a1d52aca057688807ae882fe3ea`
- `1dac3d924e8fa537bdf5ac5ba7379253ad97abf5`
- `c2ab72970810a9d2efd23c8446dde768a269ad8c`

## CI regression

The fresh-process budget CLI test copied `integrations` but omitted its real
`providers.kimi_models` dependency. It now copies `providers` too, isolates
PYTHONPATH to the fixture, and asserts the child exit status before checking
stdout. Production imports and configuration validation are unchanged.
Tests run with temporary state; no account requests or live session operations
are needed for this reconciliation.

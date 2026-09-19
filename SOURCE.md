# Upstream source provenance

PA Server was rebuilt from the adjacent `D:\stock\PA_Agent` working tree on
2026-09-19. The source tree was read in place; it was not modified.

- Source Git commit: `805723e94fbe0038bfa27c163edb6b3e6be0bf57`
- Source working tree: modified and untracked files were present.
- Copied local changes include the analysis prompt/schema/normalizer and the
  untracked OKX execution modules and tests.
- Desktop GUI files, DPAPI secrets, local settings, records, logs, virtual
  environments and trading databases were deliberately excluded.
- The Python packages `pa_agent/data` and `pa_agent/records` are source code and
  are included. Root-level `/data` and `/records` remain ignored as runtime
  storage. The Qt-only `pa_agent/data/refresh_loop.py` is intentionally omitted.
- Server adapters replace the Qt event loop, Windows credential storage and
  desktop dialogs while calling the copied analysis and OKX state machines.

## Copied-file SHA-256 values

| Source path | SHA-256 at copy time |
|---|---|
| `pa_agent/ai/prompt_assembler.py` | `3BFBD392707C983C34BBBD5FFFA00530F184B73099658A9930B165B450FA05B3` |
| `pa_agent/ai/prompts/schemas.py` | `B332EC66A31ED165BA95DA354BE42821D7A186FC6E8E6FA99B3469CE9D93239E` |
| `pa_agent/ai/stage2_normalizer.py` | `C448EF7D215B822A037741E5B3D4DB4C346146D6F772EA5558EF553245BF53B9` |
| `pa_agent/trading/okx_client.py` | `9C42B1356B951D57F7B4E75C663BC358366E9DD606034CC618CF009F2B645064` |
| `pa_agent/trading/okx_service.py` | `E3990D042252AD588D19EB5C2FA1E05DD716B3853F94F80B8EF3302D2A6FF0F7` |
| `pa_agent/trading/sizing.py` | `BAB58B7516E59C3E3AE29B2EE6F63275BB523D525AACF5B1840827146F77BDD5` |
| `pa_agent/trading/thesis_store.py` | `59D3192E695428DB127896C3D0A44D41C217F25D9EA128C8138974B4A5E181BD` |

The complete dirty-file inventory captured during the rebuild is retained in
the implementing Git commit and can be regenerated from the source tree while
it remains unchanged.

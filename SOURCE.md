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
- The 2026-09-19 TP amendment update was synchronized from the same dirty source
  tree. Server-specific watch ownership fields extend `okx_service.py` and
  `thesis_store.py`; the table below records the upstream bytes before adapting.

## Copied-file SHA-256 values

| Source path | SHA-256 at copy time |
|---|---|
| `pa_agent/ai/prompt_assembler.py` | `6D31ED5A3B9C4B4D1D4D8EACF34F397A648DAEF35F499E97707C930324A42CCD` |
| `pa_agent/ai/prompts/schemas.py` | `C62507D653F854CD4E53D2BA04100D59D9C606D34D4BC5C84503782905D8F2B5` |
| `pa_agent/ai/stage2_normalizer.py` | `AFCC6A6BF5A0B2A976DF8D6BB341B70D7879700A5562E5C8ADDF267467DF7801` |
| `pa_agent/orchestrator/two_stage.py` | `928C4A247B037386EFAA0E191A57399264AAC2B1C1BA29A4D75D7D4831ACBC3D` |
| `pa_agent/trading/okx_client.py` | `524D13B159874F1D9E3596120061AE7EAAA29AD7B71EDEE10D37A1B462C59241` |
| `pa_agent/trading/okx_service.py` | `692F0B8AA3FD917E7374E236B9B688FF205F2BAD98F504D99D2E1A9665F38648` |
| `pa_agent/trading/sizing.py` | `BAB58B7516E59C3E3AE29B2EE6F63275BB523D525AACF5B1840827146F77BDD5` |
| `pa_agent/trading/thesis_store.py` | `7D6CAE697E848BCB4EB6904F811134AA6C2CC0B5602D76E67693F61E4DC68BB6` |

The complete dirty-file inventory captured during the rebuild is retained in
the implementing Git commit and can be regenerated from the source tree while
it remains unchanged.

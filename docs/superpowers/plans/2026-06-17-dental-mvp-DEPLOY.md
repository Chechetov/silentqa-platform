# Dental MVP — deploy to prod SilentQA (fulldent)

Prod platform: `/root/projects/silentqa` (:8007). Do these in order.

1. Merge `dental-mvp-fulldent` → `multi-tenant-core-phase1`; deploy code to `/root/projects/silentqa` (pull/rsync per existing process). Includes `companies/dental.json`.
2. Add to prod worker `.env`: `ELEVENLABS_API_KEY=<rotated key>` (env only — never commit). Optional: `ELEVENLABS_STT_MODEL=scribe_v2`, `ELEVENLABS_LANGUAGE=ru`.
3. Point the tenant: `UPDATE shared.tenants SET company_config_id='dental' WHERE slug='fulldent';` (or via platform admin). Then invalidate the registry cache / restart backend.
4. Restart worker + backend (so company_config + ASR engine reload; `_TENANT_COMPANY_CACHE`/`_cache` are process-level).
5. E2E as a fulldent user: upload a dental appointment via `#upload`, wait for processing, open the call → verify transcript (ElevenLabs speakers), QA (dental criteria, N/A where inapplicable), and «Карта приёма» (incl. зубная формула).
6. Rotate the ElevenLabs key used during testing (it was pasted in chat).

Rollback: set `company_config_id` back to previous; remove `ELEVENLABS_API_KEY`. realestate is unaffected throughout (no `card_extraction`, scenarios keep their `prompt` → V4 unchanged).

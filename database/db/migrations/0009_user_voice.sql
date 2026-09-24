-- 0009: the user's Cairn voice (UC-REG-12, spec 1.2.0).
--
-- The personality step now offers the four voices in voices/manifest.yaml
-- instead of gentle, steady, and straightforward. users.voice replaces
-- users.personality. A voice changes tone only. The crisis protocol, AI
-- disclosure, attorney referrals, and citations live in voices/core.md and
-- apply to every voice.
--
-- The ids below must match voices/manifest.yaml and cairn_api.schemas.Voice.
-- api/tests/test_voices.py enforces it. Adding a voice means a new migration
-- that replaces users_voice_known.

ALTER TABLE cairn.users
  ADD COLUMN voice text NOT NULL DEFAULT 'steady_direct'
    CONSTRAINT users_voice_known
    CHECK (voice IN ('steady_direct', 'warm_patient', 'brisk_businesslike', 'plain_practical'));

-- Keep each existing choice at the closest voice. brisk_businesslike is new,
-- so nobody is moved to it.
UPDATE cairn.users SET voice = CASE personality
  WHEN 'gentle'          THEN 'warm_patient'
  WHEN 'straightforward' THEN 'plain_practical'
  ELSE 'steady_direct'
END;

-- Dropping the column also drops the app's UPDATE grant on it.
ALTER TABLE cairn.users DROP COLUMN personality;

GRANT UPDATE (voice) ON cairn.users TO cairn_app;

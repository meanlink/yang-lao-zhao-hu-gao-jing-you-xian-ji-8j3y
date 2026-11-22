-- 巡房观察记错撤销：原观察软删除留档，告警按剩余观察重算。
-- 撤销唯一的已评分观察后，告警可回到未分级的待复核状态，等待剩余观察重新评分。

-- 告警关闭来源：接班员签收归档，或撤销后无剩余观察而结束。
DO $$ BEGIN
    ALTER TABLE alerts ADD COLUMN closed_reason text;
EXCEPTION WHEN duplicate_column THEN NULL; END $$;
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_closed_reason_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_closed_reason_check
    CHECK (closed_reason IS NULL OR closed_reason IN ('signed','revoked'));

-- 001 中未命名 CHECK 的自动名通常为 alerts_check1 / alerts_check2，先清理再统一命名。
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_check1;
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_check2;
ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_status_priority_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_status_priority_check
    CHECK (status NOT IN ('confirmed','handover','closed') OR priority IS NOT NULL);

-- 观察增加 revoked 状态。
ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_status_check;
ALTER TABLE observations ADD CONSTRAINT observations_status_check
    CHECK (status IN ('incomplete','queued','scored','revoked'));

-- 原 observations_check 名称会与外键自动命名冲突，改用独立名称。
ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_check;
ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_check1;
ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_signals_check;
ALTER TABLE observations ADD CONSTRAINT observations_signals_check
    CHECK (status = 'revoked' OR
           (status = 'incomplete') = (missed_round IS NULL OR repeated_call IS NULL OR
            transfer_support IS NULL OR unfinished_task IS NULL));

ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_check2;
ALTER TABLE observations DROP CONSTRAINT IF EXISTS observations_scored_check;
ALTER TABLE observations ADD CONSTRAINT observations_scored_check
    CHECK (status <> 'scored' OR (result IS NOT NULL AND scored_at IS NOT NULL));

CREATE TABLE IF NOT EXISTS observation_revocations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL UNIQUE REFERENCES observations,
    alert_id bigint NOT NULL REFERENCES alerts,
    revoked_by bigint NOT NULL REFERENCES staff,
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 600),
    previous_status text NOT NULL CHECK (previous_status IN ('incomplete','queued','scored')),
    previous_priority smallint CHECK (previous_priority BETWEEN 1 AND 3),
    remaining_observations integer NOT NULL CHECK (remaining_observations >= 0),
    superseded_handover_id bigint REFERENCES handovers,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS revoked_observations_archive ON observations(alert_id, id) WHERE status = 'revoked';

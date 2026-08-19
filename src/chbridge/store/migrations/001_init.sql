-- =============================================================================
-- 001_init : 채널 브릿지 초기 스키마
--
-- 설계 근거는 실행계획서.md 4장 참조.
-- 핵심 원칙 두 가지를 애플리케이션 로직이 아니라 DB 제약으로 강제한다.
--   1) 한 채널은 하나의 브릿지에만 속한다        -> 토폴로지 사이클 방지 (PRD 5.1)
--   2) 같은 이벤트는 두 번 처리되지 않는다        -> 백필-스트림 중복 차단 (PRD 5.9)
-- =============================================================================

-- ----------------------------------------------------------------- 브릿지 정의
CREATE TABLE bridges (
    id          TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'active',
    options     JSONB       NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT bridges_status_valid CHECK (status IN ('active', 'paused'))
);

CREATE TABLE endpoints (
    id            TEXT PRIMARY KEY,
    bridge_id     TEXT NOT NULL REFERENCES bridges (id) ON DELETE CASCADE,
    platform      TEXT NOT NULL,
    workspace_id  TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    alias         TEXT NOT NULL,
    CONSTRAINT endpoints_platform_valid CHECK (platform IN ('mattermost', 'slack')),
    -- ★ 한 채널이 두 개 이상의 브릿지에 속하는 것을 원천 차단한다.
    --   A<->B, B<->C 를 각각 등록하면 A의 메시지가 C까지 2홉 전파된다. (PRD 5.1)
    CONSTRAINT endpoints_channel_unique UNIQUE (platform, workspace_id, channel_id)
);

CREATE INDEX endpoints_bridge ON endpoints (bridge_id);

-- ------------------------------------------------------- MessageLink (시스템의 심장)
-- Origin 과 Replica 들을 group_id 로 묶는다. 편집·삭제·리액션·쓰레드 전달이
-- 모두 이 테이블 조회에 의존한다.
CREATE TABLE message_links (
    id            BIGSERIAL PRIMARY KEY,
    bridge_id     TEXT        NOT NULL REFERENCES bridges (id) ON DELETE CASCADE,
    group_id      UUID        NOT NULL,
    platform      TEXT        NOT NULL,
    workspace_id  TEXT        NOT NULL,
    channel_id    TEXT        NOT NULL,
    message_id    TEXT        NOT NULL,
    is_origin     BOOLEAN     NOT NULL,
    author_ref    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- ★ workspace_id 가 반드시 포함되어야 한다.
    --   MM<->MM 에서는 platform 이 양쪽 모두 'mattermost' 이므로,
    --   workspace_id 가 없으면 서로 다른 서버의 동일 channel_id 가 충돌한다.
    CONSTRAINT message_links_ref_unique
        UNIQUE (platform, workspace_id, channel_id, message_id)
);

-- 핫패스 1: 수신 메시지가 Replica 인지 판정 (LoopGuard 방어 ②)
--           -> message_links_ref_unique 인덱스가 담당
-- 핫패스 2: group_id 로 반대편 메시지 찾기 (편집/삭제/리액션/쓰레드)
CREATE INDEX message_links_group ON message_links (group_id);
CREATE INDEX message_links_bridge_recent ON message_links (bridge_id, created_at DESC);

-- --------------------------------------------------- 영속 큐 + 멱등 (PRD 5.9)
-- 수신 이벤트를 먼저 여기에 기록한 뒤 처리한다.
-- 이것이 "재시작·단절 후 유실 0건"(NFR-2)을 보장하는 지점이다.
CREATE TABLE event_inbox (
    id            BIGSERIAL PRIMARY KEY,
    platform      TEXT        NOT NULL,
    workspace_id  TEXT        NOT NULL,
    event_key     TEXT        NOT NULL,
    channel_id    TEXT        NOT NULL,
    bridge_id     TEXT,
    payload       JSONB       NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',
    attempts      INT         NOT NULL DEFAULT 0,
    last_error    TEXT,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ,
    CONSTRAINT event_inbox_status_valid
        CHECK (status IN ('pending', 'processing', 'done', 'dead')),
    -- ★ 이벤트 재전송과 백필-스트림 중복 구간을 동시에 차단한다.
    --   이것 없이 백필을 붙이면 재연결마다 메시지가 중복 게시된다.
    CONSTRAINT event_inbox_key_unique UNIQUE (platform, workspace_id, event_key)
);

-- 채널 단위 FIFO 클레임용. 처리 대상만 좁게 인덱싱한다.
CREATE INDEX event_inbox_claim ON event_inbox (channel_id, id)
    WHERE status = 'pending';
CREATE INDEX event_inbox_stuck ON event_inbox (status, received_at)
    WHERE status = 'processing';
CREATE INDEX event_inbox_dead ON event_inbox (received_at DESC)
    WHERE status = 'dead';

-- ------------------------------------------- 재연결 백필 커서 (PRD 5.9 / FR-8)
CREATE TABLE sync_cursors (
    endpoint_id   TEXT PRIMARY KEY REFERENCES endpoints (id) ON DELETE CASCADE,
    cursor_value  TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------- 파일 재업로드 방지 (PRD 5.8, Phase 3)
CREATE TABLE file_links (
    id            BIGSERIAL PRIMARY KEY,
    group_id      UUID        NOT NULL,
    src_platform  TEXT        NOT NULL,
    src_file_id   TEXT        NOT NULL,
    dst_endpoint  TEXT        NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    dst_file_id   TEXT        NOT NULL,
    bytes         BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT file_links_unique UNIQUE (src_platform, src_file_id, dst_endpoint)
);

-- ------------------------------------- 리액션 집계 상태 (PRD 5.5, Phase 3)
-- 브릿지 Bot 단일 계정으로 리액션을 달기 때문에 개수가 1로 접힌다.
-- 원본에서 누른 사용자 목록을 여기서 추적해, 마지막 사용자가 제거했을 때만
-- 반대편 리액션을 제거한다.
CREATE TABLE reaction_state (
    group_id   UUID   NOT NULL,
    emoji      TEXT   NOT NULL,
    src_users  TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (group_id, emoji)
);

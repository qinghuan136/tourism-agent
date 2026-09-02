-- Conversation 保留原始事实，RAG Chunk 只保存可重建的语义检索数据。
CREATE EXTENSION IF NOT EXISTS vector;

-- 后续接入索引流程时，同一次请求的 User/Assistant 消息共享 exchange_id。
-- 当前先允许为空，避免迁移影响既有数据和尚未接入 Exchange 写入的 API。
ALTER TABLE tourism_agent.conversation_messages
    ADD COLUMN exchange_id uuid;

-- 复合唯一约束既供 Chunk 外键校验 Trip 归属，也能继续支持近期消息反向扫描。
DROP INDEX tourism_agent.idx_conversation_messages_trip_recent;

ALTER TABLE tourism_agent.conversation_messages
    ADD CONSTRAINT uq_conversation_messages_trip_id_id
    UNIQUE (trip_id, id);

CREATE UNIQUE INDEX uq_conversation_messages_trip_exchange_role
    ON tourism_agent.conversation_messages (trip_id, exchange_id, role)
    WHERE exchange_id IS NOT NULL;

CREATE TABLE tourism_agent.conversation_rag_chunks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id uuid NOT NULL
        REFERENCES tourism_agent.trips(id)
        ON DELETE CASCADE,
    exchange_id uuid NOT NULL,
    user_message_id bigint NOT NULL,
    assistant_message_id bigint NOT NULL,

    -- retrieval_text 是语义增强或压缩后的检索文本，不替代原始 Conversation。
    retrieval_text text NOT NULL CHECK (btrim(retrieval_text) <> ''),
    retrieval_text_sha256 text NOT NULL CHECK (
        retrieval_text_sha256 ~ '^[0-9a-f]{64}$'
    ),
    source_token_count integer NOT NULL CHECK (source_token_count > 0),
    retrieval_token_count integer NOT NULL CHECK (retrieval_token_count > 0),
    enhancement_model text NOT NULL CHECK (btrim(enhancement_model) <> ''),
    enhancement_version smallint NOT NULL DEFAULT 1 CHECK (enhancement_version > 0),

    -- 当前项目固定使用该模型和维度，禁止同表混入不兼容向量。
    embedding_model text NOT NULL DEFAULT 'qwen3.7-text-embedding',
    embedding vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_conversation_rag_chunks_trip_exchange
        UNIQUE (trip_id, exchange_id),
    CONSTRAINT ck_conversation_rag_chunks_message_order
        CHECK (user_message_id < assistant_message_id),
    CONSTRAINT ck_conversation_rag_chunks_embedding_model
        CHECK (embedding_model = 'qwen3.7-text-embedding'),
    CONSTRAINT fk_conversation_rag_chunks_user_message
        FOREIGN KEY (trip_id, user_message_id)
        REFERENCES tourism_agent.conversation_messages (trip_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_conversation_rag_chunks_assistant_message
        FOREIGN KEY (trip_id, assistant_message_id)
        REFERENCES tourism_agent.conversation_messages (trip_id, id)
        ON DELETE CASCADE
);

// Package oauthsession provides a shared Redis-backed storage backend for
// per-platform OAuth session stores.
//
// Each platform (claude/openai/grok/gemini/antigravity) keeps its own
// SessionStore with a platform-specific OAuthSession type. In a multi-replica
// deployment an in-memory map fails intermittently: the authorize step and the
// code-exchange step can land on different pods, so the session written on pod
// A is missing on pod B ("session not found or expired"). Backing the store
// with Redis makes sessions shared across replicas.
//
// This package is the single place that imports go-redis for the OAuth session
// stores, keeping the redis dependency out of the low-level oauth pkgs.
package oauthsession

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"
)

// redisOpTimeout bounds each individual Redis call so a slow/unreachable Redis
// cannot block an OAuth request indefinitely.
const redisOpTimeout = 3 * time.Second

// RedisBackend stores OAuth sessions as JSON in Redis with a fixed TTL.
//
// A nil *RedisBackend is not valid; callers hold a nil field and branch on it
// instead (nil field => use the in-memory map).
type RedisBackend struct {
	rdb    *redis.Client
	prefix string
	ttl    time.Duration
}

// NewRedisBackend creates a Redis-backed store. prefix namespaces keys per
// platform (e.g. "oauth:session:openai:"); ttl mirrors the in-memory
// SessionTTL. Returns nil when rdb is nil so callers transparently fall back to
// in-memory storage.
func NewRedisBackend(rdb *redis.Client, prefix string, ttl time.Duration) *RedisBackend {
	if rdb == nil {
		return nil
	}
	return &RedisBackend{rdb: rdb, prefix: prefix, ttl: ttl}
}

func (b *RedisBackend) key(sessionID string) string {
	return b.prefix + sessionID
}

// Set marshals session to JSON and stores it under sessionID with the backend
// TTL. Errors are logged and swallowed to match the fire-and-forget signature
// of the in-memory Set.
func (b *RedisBackend) Set(sessionID string, session any) {
	data, err := json.Marshal(session)
	if err != nil {
		slog.Error("oauth_session_redis_marshal_failed", "error", err)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), redisOpTimeout)
	defer cancel()
	if err := b.rdb.Set(ctx, b.key(sessionID), data, b.ttl).Err(); err != nil {
		slog.Error("oauth_session_redis_set_failed", "error", err)
	}
}

// Get loads the session for sessionID into dst (a pointer). It returns true
// only when the key exists and unmarshals cleanly. A missing key, a Redis
// error, or a decode error all return false, degrading to "not found" — the
// same outcome the in-memory store produces for an unknown/expired session.
func (b *RedisBackend) Get(sessionID string, dst any) bool {
	ctx, cancel := context.WithTimeout(context.Background(), redisOpTimeout)
	defer cancel()
	data, err := b.rdb.Get(ctx, b.key(sessionID)).Bytes()
	if err != nil {
		if err != redis.Nil {
			slog.Error("oauth_session_redis_get_failed", "error", err)
		}
		return false
	}
	if err := json.Unmarshal(data, dst); err != nil {
		slog.Error("oauth_session_redis_unmarshal_failed", "error", err)
		return false
	}
	return true
}

// Delete removes the session for sessionID. Errors are logged and swallowed.
func (b *RedisBackend) Delete(sessionID string) {
	ctx, cancel := context.WithTimeout(context.Background(), redisOpTimeout)
	defer cancel()
	if err := b.rdb.Del(ctx, b.key(sessionID)).Err(); err != nil {
		slog.Error("oauth_session_redis_del_failed", "error", err)
	}
}

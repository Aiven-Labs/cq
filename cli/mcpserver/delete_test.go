package mcpserver

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/stretchr/testify/require"

	cq "github.com/mozilla-ai/cq/sdk/go"
)

func TestHandleDelete(t *testing.T) {
	t.Parallel()

	t.Run("falls back to remote tier on local not found", func(t *testing.T) {
		t.Parallel()

		calls := 0
		s := New(&mockClient{
			deleteFn: func(_ context.Context, ku cq.KnowledgeUnit) (cq.DeleteResult, error) {
				calls++
				if calls == 1 {
					require.Equal(t, cq.Local, ku.Tier)
					return cq.DeleteResult{}, cq.ErrNotFound
				}

				require.Equal(t, cq.Private, ku.Tier)
				return cq.DeleteResult{UnitID: ku.ID, Status: "deleted"}, nil
			},
		}, "test")

		result, err := s.HandleDelete(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{Name: "delete", Arguments: map[string]any{"unit_id": "ku_1"}},
		})
		require.NoError(t, err)
		require.False(t, result.IsError)
		require.Equal(t, 2, calls)

		text := result.Content[0].(mcp.TextContent).Text
		var dr cq.DeleteResult
		require.NoError(t, json.Unmarshal([]byte(text), &dr))
		require.Equal(t, "ku_1", dr.UnitID)
		require.Equal(t, "deleted", dr.Status)
	})

	t.Run("errors when unit id is missing", func(t *testing.T) {
		t.Parallel()

		s := New(&mockClient{}, "test")
		result, err := s.HandleDelete(context.Background(), mcp.CallToolRequest{
			Params: mcp.CallToolParams{Name: "delete", Arguments: map[string]any{}},
		})
		require.NoError(t, err)
		require.True(t, result.IsError)
	})
}

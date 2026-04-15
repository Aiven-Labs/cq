package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/mark3labs/mcp-go/mcp"

	cq "github.com/mozilla-ai/cq/sdk/go"
)

// DeleteTool returns the MCP tool definition for delete.
func DeleteTool() mcp.Tool {
	return mcp.NewTool("delete",
		mcp.WithDescription("Permanently delete a knowledge unit from the store."),
		mcp.WithString("unit_id",
			mcp.Required(),
			mcp.Description("ID of the knowledge unit to delete."),
		),
	)
}

// HandleDelete permanently removes a knowledge unit.
func (s *Server) HandleDelete(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	unitID, err := req.RequireString("unit_id")
	if err != nil {
		return mcp.NewToolResultError("unit_id is required"), nil
	}

	result, err := s.client.Delete(ctx, cq.KnowledgeUnit{ID: unitID, Tier: cq.Local})
	if errors.Is(err, cq.ErrNotFound) {
		result, err = s.client.Delete(ctx, cq.KnowledgeUnit{ID: unitID, Tier: cq.Private})
	}
	if err != nil {
		return nil, fmt.Errorf("deleting: %w", err)
	}

	data, err := json.Marshal(result)
	if err != nil {
		return nil, fmt.Errorf("encoding result: %w", err)
	}

	return mcp.NewToolResultText(string(data)), nil
}

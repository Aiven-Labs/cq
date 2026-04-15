import { useState, useEffect, useCallback, useMemo } from "react";
import { api } from "../api";
import { DomainTags } from "../components/DomainTags";
import { KnowledgeUnitModal } from "../components/KnowledgeUnitModal";
import { timeAgo } from "../utils";
import type { ReviewItem, ReviewStatsResponse } from "../types";

function confidenceColor(c: number): string {
  if (c < 0.3) return "text-red-600";
  if (c < 0.5) return "text-amber-600";
  if (c < 0.7) return "text-yellow-500";
  return "text-green-600";
}

function confidenceBarColor(c: number): string {
  if (c < 0.3) return "bg-red-500";
  if (c < 0.5) return "bg-amber-500";
  if (c < 0.7) return "bg-yellow-500";
  return "bg-green-500";
}

export function ExplorerPage() {
  const [stats, setStats] = useState<ReviewStatsResponse | null>(null);
  const [units, setUnits] = useState<ReviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selectedDomains, setSelectedDomains] = useState<Set<string>>(
    new Set(),
  );
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [selectedUnitId, setSelectedUnitId] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    Promise.all([api.reviewStats(), api.listUnits({ status: "approved" })])
      .then(([statsData, unitsData]) => {
        if (ignore) return;
        setStats(statsData);
        setUnits(unitsData);
      })
      .catch(() => {
        if (!ignore) setError("Failed to load knowledge base");
      });
    return () => {
      ignore = true;
    };
  }, []);

  const toggleDomain = useCallback((domain: string) => {
    setSelectedDomains((prev) => {
      const next = new Set(prev);
      if (next.has(domain)) next.delete(domain);
      else next.add(domain);
      return next;
    });
  }, []);

  const clearFilters = useCallback(() => {
    setSearch("");
    setSelectedDomains(new Set());
  }, []);

  const filteredUnits = useMemo(() => {
    if (!units) return [];
    const needle = search.toLowerCase().trim();
    return units.filter((item) => {
      const ku = item.knowledge_unit;
      if (selectedDomains.size > 0) {
        const domainSet = new Set(ku.domains);
        for (const d of selectedDomains) {
          if (!domainSet.has(d)) return false;
        }
      }
      if (needle) {
        const haystack = [
          ku.insight.summary,
          ku.insight.detail,
          ku.insight.action,
          ...ku.domains,
          ...ku.context.languages,
          ...ku.context.frameworks,
        ]
          .join(" ")
          .toLowerCase();
        return haystack.includes(needle);
      }
      return true;
    });
  }, [units, search, selectedDomains]);

  const filteredDomains = useMemo(() => {
    if (!stats) return [];
    const needle = search.toLowerCase().trim();
    return Object.entries(stats.domains)
      .filter(([domain]) => !needle || domain.toLowerCase().includes(needle))
      .sort(([, a], [, b]) => b - a);
  }, [stats, search]);

  const maxDomainCount = useMemo(() => {
    if (filteredDomains.length === 0) return 1;
    return Math.max(...filteredDomains.map(([, c]) => c));
  }, [filteredDomains]);

  const hasFilters = search.length > 0 || selectedDomains.size > 0;

  if (!stats && !units && !error) {
    return (
      <div className="space-y-6" data-testid="explorer-skeleton">
        <div className="h-10 w-full animate-pulse bg-gray-200 rounded-lg" />
        <div className="flex flex-wrap gap-2">
          {[1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="h-7 w-20 animate-pulse bg-gray-200 rounded-full"
            />
          ))}
        </div>
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            className="h-24 w-full animate-pulse bg-gray-100 rounded-lg"
          />
        ))}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 text-center">
          <p className="text-red-600 text-sm font-medium">{error}</p>
        </div>
      )}

      <div className="relative">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search knowledge units..."
          aria-label="Search knowledge units"
          className="w-full rounded-lg border border-gray-300 bg-white px-4 py-2.5 text-sm text-gray-900 placeholder-gray-400 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
        />
        {hasFilters && (
          <button
            onClick={clearFilters}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-600"
          >
            Clear
          </button>
        )}
      </div>

      {filteredDomains.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">
            Domains
            {selectedDomains.size > 0 && (
              <span className="ml-2 text-indigo-500 normal-case font-normal">
                {selectedDomains.size} selected
              </span>
            )}
          </h3>
          <div className="flex flex-wrap gap-2" data-testid="domain-cloud">
            {filteredDomains.map(([domain, count]) => {
              const isSelected = selectedDomains.has(domain);
              const scale = 0.75 + 0.25 * (count / maxDomainCount);
              return (
                <button
                  key={domain}
                  onClick={() => toggleDomain(domain)}
                  className={`inline-flex items-center gap-1 rounded-full px-3 py-1 font-medium transition-colors ${
                    isSelected
                      ? "bg-indigo-600 text-white"
                      : "bg-gray-100 text-gray-700 hover:bg-indigo-100 hover:text-indigo-700"
                  }`}
                  style={{ fontSize: `${scale}rem` }}
                  data-testid={`domain-tag-${domain}`}
                >
                  {domain}
                  <span
                    className={`${isSelected ? "text-indigo-200" : "text-gray-400"}`}
                  >
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {units && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-gray-500" data-testid="result-count">
            {filteredUnits.length}{" "}
            {filteredUnits.length === 1 ? "unit" : "units"}
            {hasFilters && ` of ${units.length}`}
          </p>
        </div>
      )}

      {filteredUnits.length === 0 && units && (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
          <p className="text-gray-400 text-sm" data-testid="empty-state">
            {hasFilters
              ? "No knowledge units match your filters."
              : "No approved knowledge units yet."}
          </p>
          {hasFilters && (
            <button
              onClick={clearFilters}
              className="mt-2 text-sm text-indigo-500 hover:text-indigo-700"
            >
              Clear filters
            </button>
          )}
        </div>
      )}

      <div className="space-y-3" data-testid="knowledge-list">
        {filteredUnits.map((item) => {
          const ku = item.knowledge_unit;
          const isExpanded = expandedId === ku.id;
          return (
            <div
              key={ku.id}
              className={`bg-white rounded-lg border transition-colors ${
                isExpanded
                  ? "border-indigo-300 shadow-sm"
                  : "border-gray-200 hover:border-gray-300"
              }`}
              data-testid={`knowledge-card-${ku.id}`}
            >
              <button
                className="w-full text-left p-4"
                onClick={() => setExpandedId(isExpanded ? null : ku.id)}
                aria-expanded={isExpanded}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-gray-900 mb-1">
                      {ku.insight.summary}
                    </h3>
                    <DomainTags domains={ku.domains} />
                  </div>
                  <div className="flex items-center gap-3 shrink-0">
                    <div className="flex items-center gap-1.5">
                      <div className="w-16 h-1.5 bg-gray-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${confidenceBarColor(ku.evidence.confidence)}`}
                          style={{
                            width: `${ku.evidence.confidence * 100}%`,
                          }}
                        />
                      </div>
                      <span
                        className={`text-xs font-medium ${confidenceColor(ku.evidence.confidence)}`}
                      >
                        {ku.evidence.confidence.toFixed(2)}
                      </span>
                    </div>
                    <svg
                      className={`w-4 h-4 text-gray-400 transition-transform ${isExpanded ? "rotate-180" : ""}`}
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M19 9l-7 7-7-7"
                      />
                    </svg>
                  </div>
                </div>
              </button>

              {isExpanded && (
                <div className="px-4 pb-4 space-y-3 border-t border-gray-100 pt-3">
                  <p className="text-gray-600 text-sm leading-relaxed">
                    {ku.insight.detail}
                  </p>

                  <div className="border-l-3 rounded-r-lg px-4 py-3 bg-indigo-50 border-indigo-500">
                    <span className="text-xs font-semibold uppercase tracking-wide text-indigo-500">
                      Action
                    </span>
                    <p className="text-gray-800 text-sm mt-1">
                      {ku.insight.action}
                    </p>
                  </div>

                  <div className="flex flex-wrap gap-4 text-sm text-gray-500">
                    <span>
                      Confirmations:{" "}
                      <strong className="text-gray-800">
                        {ku.evidence.confirmations}
                      </strong>
                    </span>
                    {ku.evidence.first_observed && (
                      <span>
                        Observed: {timeAgo(ku.evidence.first_observed)}
                      </span>
                    )}
                    {ku.created_by && (
                      <span>
                        By:{" "}
                        <strong className="text-gray-700">
                          {ku.created_by}
                        </strong>
                      </span>
                    )}
                  </div>

                  {(ku.context.languages.length > 0 ||
                    ku.context.frameworks.length > 0) && (
                    <div className="text-xs text-gray-500">
                      {ku.context.languages.length > 0 && (
                        <span>
                          Languages: {ku.context.languages.join(", ")}
                        </span>
                      )}
                      {ku.context.languages.length > 0 &&
                        ku.context.frameworks.length > 0 && (
                          <span className="mx-1">&middot;</span>
                        )}
                      {ku.context.frameworks.length > 0 && (
                        <span>
                          Frameworks: {ku.context.frameworks.join(", ")}
                        </span>
                      )}
                    </div>
                  )}

                  <div className="flex items-center justify-between pt-2 border-t border-gray-100">
                    <span className="text-xs text-gray-400 font-mono">
                      {ku.id}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelectedUnitId(ku.id);
                      }}
                      className="text-xs text-indigo-500 hover:text-indigo-700 font-medium"
                    >
                      Full details
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {selectedUnitId && (
        <KnowledgeUnitModal
          key={selectedUnitId}
          unitId={selectedUnitId}
          onClose={() => setSelectedUnitId(null)}
        />
      )}
    </div>
  );
}

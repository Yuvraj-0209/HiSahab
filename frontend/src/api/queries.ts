/* Reads through TanStack Query (Phase 23 D6).
 *
 * Every GET is keyed ["api", path, query], so one prefix invalidates everything after a write:
 * money screens are small and writes are rare, and refetching what is on screen is the honest
 * way to show the server's figures rather than a client-side guess at them (§14 forbids the
 * client computing a total). A write never patches a figure into the cache by hand.
 */

import { keepPreviousData, type QueryKey, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { api, ApiError, type Query } from "./client";

export function apiKey(path: string, query?: Query): QueryKey {
  return query ? ["api", path, query] : ["api", path];
}

export interface ApiQueryOptions {
  enabled?: boolean;
  /** Error codes that mean "there is nothing here" rather than "something failed" -- e.g.
   * NO_OPEN_SHIFT from /shifts/current. They resolve to `null`. */
  absentOn?: string[];
  /** While a NEW query's answer is loading, keep showing the previous answer (Phase 26), and
   * say so through `isPlaceholderData` -- the caller must mark it as stale, never present it
   * as the new answer. For a panel whose subject changes under a pointer: the alternative is a
   * blank frame every time, which reads as a flicker. */
  keepPrevious?: boolean;
}

export function useApiQuery<T>(path: string, query?: Query, options: ApiQueryOptions = {}): UseQueryResult<T | null> {
  const { enabled = true, absentOn, keepPrevious = false } = options;
  return useQuery<T | null>({
    queryKey: apiKey(path, query),
    enabled,
    ...(keepPrevious ? { placeholderData: keepPreviousData } : {}),
    queryFn: async () => {
      try {
        return await api.get<T>(path, query);
      } catch (error) {
        if (absentOn && error instanceof ApiError && absentOn.includes(error.code)) return null;
        throw error;
      }
    },
  });
}

/** After any write: refetch everything on screen. */
export function useRefreshApi(): () => Promise<void> {
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: ["api"] });
}

"use client";
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { getOptionChain, getExpiries } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import { useMarketStore } from "@/lib/store";

/**
 * Option chain for the selected underlying/expiry. Safe to call from several components: the
 * queries are shared by key and the store sync is idempotent. Field selectors only (E11).
 */
export function useOptionChain() {
  const selectedUnderlying = useMarketStore((s) => s.selectedUnderlying);
  const selectedExpiry     = useMarketStore((s) => s.selectedExpiry);
  const setOptionChain     = useMarketStore((s) => s.setOptionChain);
  const setExpiry          = useMarketStore((s) => s.setExpiry);

  const expiriesQuery = useQuery({
    queryKey: queryKeys.expiries(selectedUnderlying),
    queryFn:  () => getExpiries(selectedUnderlying),
    staleTime: 5 * 60 * 1000,
  });
  const expiries = expiriesQuery.data;

  // Auto-select the nearest expiry once the list arrives.
  useEffect(() => {
    if (Array.isArray(expiries) && expiries.length && !selectedExpiry) setExpiry(expiries[0]);
  }, [expiries, selectedExpiry, setExpiry]);

  // Enabled once an expiry is known; if the expiries call itself fails, fetch the backend's default
  // (nearest) expiry anyway so the chain surface reports its own error instead of a stuck skeleton.
  const query = useQuery({
    queryKey: queryKeys.optionChain(selectedUnderlying, selectedExpiry),
    queryFn:  () => getOptionChain(selectedUnderlying, selectedExpiry ?? undefined),
    enabled:  !!selectedExpiry || expiriesQuery.isError,
    refetchInterval: 5_000,
    staleTime: 2_000,
    // Keep the previous chain visible across an expiry change so the table doesn't flash empty —
    // but never show one underlying's chain under another's selector.
    placeholderData: (prev) => (prev && prev.underlying?.toUpperCase() === selectedUnderlying ? prev : undefined),
  });

  // Sync to the store (InfoPanel reads it) — runs only when the data reference changes.
  useEffect(() => {
    setOptionChain(query.data ?? null);
  }, [query.data, setOptionChain]);

  return {
    query,
    chain:    query.data,
    expiries,
    expiriesQuery,
    isLoading: query.isPending,
    error:    query.error ?? expiriesQuery.error ?? null,
    refetch:  query.refetch,
  };
}

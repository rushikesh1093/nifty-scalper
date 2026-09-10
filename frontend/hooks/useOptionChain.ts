"use client";
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { getOptionChain, getExpiries } from "@/lib/api";
import { useMarketStore } from "@/lib/store";

export function useOptionChain() {
  const { selectedUnderlying, selectedExpiry, setOptionChain, setExpiry } = useMarketStore();

  // Fetch expiries
  const { data: expiries } = useQuery({
    queryKey: ["expiries", selectedUnderlying],
    queryFn:  () => getExpiries(selectedUnderlying),
    staleTime: 5 * 60 * 1000,
  });

  // Auto-select first expiry
  useEffect(() => {
    if (expiries?.length && !selectedExpiry) {
      setExpiry(expiries[0]);
    }
  }, [expiries, selectedExpiry, setExpiry]);

  // Fetch option chain — write to store directly in onSuccess to avoid
  // an extra render cycle from a separate useEffect
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["option-chain", selectedUnderlying, selectedExpiry],
    queryFn:  () => getOptionChain(selectedUnderlying, selectedExpiry ?? undefined),
    enabled:  !!selectedExpiry,
    refetchInterval: 5000,
    staleTime: 2000,
    // Keep previous data visible while refetching so the table doesn't flash empty
    placeholderData: (prev) => prev,
  });

  // Sync to global store — runs only when data reference changes (i.e. new poll result)
  useEffect(() => {
    setOptionChain(data ?? null);
  }, [data, setOptionChain]);

  return { chain: data, expiries, isLoading, error, refetch };
}

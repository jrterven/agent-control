import { useEffect, useState } from "react";
import { subscribeToMediaQuery } from "./mediaQuery";

function queryMatches(query: string) {
  return typeof window !== "undefined" && Boolean(window.matchMedia?.(query)?.matches);
}

export function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => queryMatches(query));

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    return subscribeToMediaQuery(media, update);
  }, [query]);

  return matches;
}

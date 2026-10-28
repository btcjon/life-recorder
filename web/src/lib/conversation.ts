import type { PlayableItem } from './playback';

export type ConversationQueue = {
  label: string;
  sources: Array<{ item: PlayableItem; position: number }>;
  total: number;
  index: number;
  finished?: boolean;
};

export function freezeConversation(items: PlayableItem[], label: string): ConversationQueue | null {
  const sources = items.flatMap((item, index) => item.audioPlayable
    ? [{ item: { ...item, preferredSource: 'original' as const }, position: index + 1 }] : []);
  return sources.length ? { label, sources, total: items.length, index: 0 } : null;
}

import { expect, it } from 'vitest';
import { freezeConversation } from './conversation';
import type { PlayableItem } from './playback';

it('freezes available original sources and preserves positions across missing audio', () => {
  const source: PlayableItem = { id: 'a', kind: 'chunk', audioPlayable: true, enhancedPlayable: false, label: 'A', pinned: false };
  const items = [source, { ...source, id: 'missing', audioPlayable: false }, { ...source, id: 'b' }];
  const queue = freezeConversation(items, 'Conversation')!;
  source.id = 'changed';
  items.reverse();
  expect(queue.sources.map(source => [source.item.id, source.position])).toEqual([['a', 1], ['b', 3]]);
  expect(queue.total).toBe(3);
  expect(queue.sources.every(source => source.item.preferredSource === 'original')).toBe(true);
  expect(freezeConversation([{ ...source, audioPlayable: false }], 'Missing')).toBeNull();
});

import { Box, Code, Link, Text } from '@chakra-ui/react';
import { useMemo, type ReactNode } from 'react';
import Markdown, { type Components } from 'react-markdown';
import { Link as RouterLink } from 'react-router';
import remarkGfm from 'remark-gfm';

import { viewerPath, type Citation } from './chatApi';

/**
 * Renders an answer as Markdown (SECURITY_MODEL.md §10). The answer is model output built from untrusted
 * documents, so:
 * - raw HTML is dropped, never rendered;
 * - images are not loaded (a request could carry data out); they become a link to their address;
 * - links open in a new tab with `rel="noopener noreferrer nofollow"` and show their domain; anything but
 *   http(s) stays plain text;
 * - `[c1]` markers become links to the protected viewer, only for citations the backend validated.
 */

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}

const CITATION_PREFIX = '#citation-';
const MARKER = /\[(c\d{1,4})\]/g;
const OPAQUE = new Set(['link', 'linkReference', 'inlineCode', 'code', 'html']);

/** Turns `[c1]` in text into links to `#citation-c1`; the `a` component decides what they become. */
function remarkCitations() {
  return (tree: MdNode) => {
    citations(tree);
  };
}

function citations(node: MdNode): void {
  if (node.children === undefined || OPAQUE.has(node.type)) return;
  const merged: MdNode[] = [];
  for (const child of node.children) {
    const last = merged.at(-1);
    if (child.type === 'text' && last?.type === 'text') {
      last.value = (last.value ?? '') + (child.value ?? '');
    } else {
      merged.push(child.type === 'text' ? { ...child } : child);
    }
  }
  node.children = merged.flatMap((child) => {
    if (child.type !== 'text') {
      citations(child);
      return [child];
    }
    const value = child.value ?? '';
    const parts: MdNode[] = [];
    let position = 0;
    for (const match of value.matchAll(MARKER)) {
      if (match.index > position) parts.push({ type: 'text', value: value.slice(position, match.index) });
      const id = match[1] ?? '';
      parts.push({ type: 'link', url: `${CITATION_PREFIX}${id}`, children: [{ type: 'text', value: id }] });
      position = match.index + match[0].length;
    }
    if (position < value.length) parts.push({ type: 'text', value: value.slice(position) });
    return parts;
  });
}

function webUrl(value: string | undefined): URL | null {
  if (value === undefined) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url : null;
  } catch {
    return null;
  }
}

function CitationMarker({ citation }: { citation: Citation }) {
  const path = viewerPath(citation);
  const label = citation.citation_id.slice(1);
  if (path === null) return <sup>[{label}]</sup>;
  return (
    <Link
      as={RouterLink}
      to={path}
      title={citation.filename}
      aria-label={`Source ${label}: ${citation.filename}`}
      fontSize="xs"
      fontWeight="semibold"
      color="brand.500"
      verticalAlign="super"
      mx="1px"
    >
      [{label}]
    </Link>
  );
}

function ExternalLink({ href, children }: { href: string | undefined; children: ReactNode }) {
  const url = webUrl(href);
  if (url === null) return <>{children}</>;
  return (
    <>
      <Link href={url.href} target="_blank" rel="noopener noreferrer nofollow" color="brand.500">
        {children}
      </Link>{' '}
      <Text as="span" fontSize="xs" color="gray.500">
        ({url.hostname})
      </Text>
    </>
  );
}

function components(byId: Map<string, Citation>): Components {
  return {
    a: ({ href, children }) => {
      if (href?.startsWith(CITATION_PREFIX)) {
        const citation = byId.get(href.slice(CITATION_PREFIX.length));
        return citation !== undefined ? <CitationMarker citation={citation} /> : <>{children}</>;
      }
      return <ExternalLink href={href}>{children}</ExternalLink>;
    },
    img: ({ src, alt }) => {
      const label = `Image${alt ? `: ${alt}` : ''}`;
      const url = webUrl(typeof src === 'string' ? src : undefined);
      return url === null ? (
        <Text as="span">[{label}]</Text>
      ) : (
        <ExternalLink href={url.href}>[{label}]</ExternalLink>
      );
    },
    code: ({ children }) => <Code fontSize="sm">{children}</Code>,
    pre: ({ children }) => (
      <Box as="pre" overflowX="auto" bg="gray.50" _dark={{ bg: 'gray.800' }} p={3} borderRadius="md" my={2}>
        {children}
      </Box>
    ),
    p: ({ children }) => (
      <Text my={2} whiteSpace="pre-wrap" wordBreak="break-word">
        {children}
      </Text>
    ),
    table: ({ children }) => (
      <Box overflowX="auto" my={2}>
        <Box as="table" fontSize="sm" sx={{ 'th, td': { borderWidth: '1px', px: 2, py: 1 } }}>
          {children}
        </Box>
      </Box>
    ),
    ul: ({ children }) => (
      <Box as="ul" pl={5} my={2}>
        {children}
      </Box>
    ),
    ol: ({ children }) => (
      <Box as="ol" pl={5} my={2}>
        {children}
      </Box>
    ),
  };
}

export function AnswerMarkdown({ text, citations: cited }: { text: string; citations: Citation[] }) {
  // Stable components: new ones on every render would remount every link (and lose a click in progress).
  const rendered = useMemo(
    () => components(new Map(cited.map((citation) => [citation.citation_id, citation]))),
    [cited],
  );
  return (
    <Box className="answer-markdown" data-testid="answer">
      <Markdown remarkPlugins={[remarkGfm, remarkCitations]} skipHtml components={rendered}>
        {text}
      </Markdown>
    </Box>
  );
}

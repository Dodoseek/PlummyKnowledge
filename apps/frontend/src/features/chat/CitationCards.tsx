import { Card, CardBody, Link, SimpleGrid, Stack, Text } from '@chakra-ui/react';
import { Link as RouterLink } from 'react-router';

import { citationPlace, viewerPath, type Citation } from './chatApi';

/** The validated sources of one answer. Opening one goes through the protected viewer, which checks access
 * again (a citation in the history grants nothing). */
export function CitationCards({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) return null;
  return (
    <Stack spacing={2} mt={3}>
      <Text fontSize="xs" fontWeight="semibold" color="gray.500" textTransform="uppercase">
        Sources
      </Text>
      <SimpleGrid columns={{ base: 1, md: 2 }} spacing={2}>
        {citations.map((citation) => {
          const path = viewerPath(citation);
          const place = citationPlace(citation);
          return (
            <Card key={citation.citation_id} variant="outline" size="sm" data-testid="citation-card">
              <CardBody>
                <Stack spacing={1}>
                  <Text fontSize="sm" fontWeight="semibold" noOfLines={1} wordBreak="break-all">
                    [{citation.citation_id.slice(1)}] {citation.filename}
                  </Text>
                  {place !== null && (
                    <Text fontSize="xs" color="gray.500">
                      {place}
                    </Text>
                  )}
                  <Text fontSize="sm" color="gray.600" _dark={{ color: 'gray.300' }} noOfLines={3}>
                    {citation.snippet}
                  </Text>
                  {path !== null && (
                    <Link as={RouterLink} to={path} fontSize="sm" color="brand.500">
                      Open source
                    </Link>
                  )}
                </Stack>
              </CardBody>
            </Card>
          );
        })}
      </SimpleGrid>
    </Stack>
  );
}

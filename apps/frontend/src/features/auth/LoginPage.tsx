import {
  Alert,
  AlertDescription,
  AlertIcon,
  Button,
  Card,
  CardBody,
  FormControl,
  FormErrorMessage,
  FormLabel,
  Heading,
  Input,
  Stack,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { safeRedirectPath } from '../../shared/navigation/safeRedirect';
import { useGetSetupStatusQuery } from '../setup/setupApi';
import { useGetSessionQuery, useLoginMutation } from './authApi';

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const next = safeRedirectPath(searchParams.get('next'));
  const setup = useGetSetupStatusQuery();
  const session = useGetSessionQuery(undefined, { skip: setup.data?.status !== 'initialized' });
  const [login, { isLoading, error }] = useLoginMutation();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitted, setSubmitted] = useState(false);

  if (setup.data?.status === 'uninitialized') {
    return <Navigate to="/setup" replace />;
  }
  if (session.data !== undefined && !session.isError) {
    return <Navigate to={next} replace />;
  }
  // The form stays visible while the session is (re)checked: an existing session only redirects.
  if (setup.isLoading) {
    return <PageSpinner />;
  }

  const emailMissing = submitted && email.trim() === '';
  const passwordMissing = submitted && password === '';

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (email.trim() === '' || password === '') return;
    try {
      await login({ email: email.trim(), password }).unwrap();
      void navigate(next, { replace: true });
    } catch {
      // The server message is shown; the cleared password field is not a "missing value" error.
      setPassword('');
      setSubmitted(false);
    }
  };

  return (
    <Stack maxW="md" mx="auto" spacing={6}>
      <Heading size="lg">Sign in</Heading>
      <Card>
        <CardBody>
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <Stack spacing={4}>
              {error !== undefined && (
                <Alert status="error" borderRadius="md" role="alert">
                  <AlertIcon />
                  <AlertDescription>{messageFor(toApiError(error))}</AlertDescription>
                </Alert>
              )}
              <FormControl isRequired isInvalid={emailMissing}>
                <FormLabel>Email</FormLabel>
                <Input
                  type="email"
                  name="email"
                  autoComplete="username"
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value);
                  }}
                />
                <FormErrorMessage>Enter your email.</FormErrorMessage>
              </FormControl>
              <FormControl isRequired isInvalid={passwordMissing}>
                <FormLabel>Password</FormLabel>
                <Input
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value);
                  }}
                />
                <FormErrorMessage>Enter your password.</FormErrorMessage>
              </FormControl>
              <Button type="submit" colorScheme="brand" isLoading={isLoading}>
                Sign in
              </Button>
            </Stack>
          </form>
        </CardBody>
      </Card>
    </Stack>
  );
}

'use client';

import { useRouter } from 'next/navigation';
import { useState, type FormEvent } from 'react';

import { Logo } from '@/components/Icon';
import { createClient } from '@/lib/supabase/client';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);

    const { error: signInError } = await createClient().auth.signInWithPassword({
      email,
      password,
    });

    if (signInError) {
      setError(signInError.message);
      setPending(false);
      return;
    }

    router.replace('/');
    router.refresh();
  }

  return (
    <main className="login">
      <form className="glass login-card" onSubmit={onSubmit}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Logo />
          <h1 className="disp" style={{ fontSize: 24 }}>
            Pantheon
          </h1>
        </div>
        <p className="muted" style={{ fontSize: 14 }}>
          Sign in with the owner account.
        </p>
        <label className="login-field">
          <span className="faint">Email</span>
          <input
            className="inp"
            type="email"
            value={email}
            required
            autoComplete="username"
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <label className="login-field">
          <span className="faint">Password</span>
          <input
            className="inp"
            type="password"
            value={password}
            required
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button type="submit" className="btn pri" disabled={pending}>
          {pending ? 'Signing in…' : 'Sign in'}
        </button>
        {error ? (
          <p role="alert" style={{ color: 'var(--verm)', fontSize: 13 }}>
            {error}
          </p>
        ) : null}
      </form>
    </main>
  );
}

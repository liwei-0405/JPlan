import React, { createContext, useContext, useEffect, useState } from 'react';
import { Session, User } from '@supabase/supabase-js';
import { supabase } from '../lib/supabase';

// Capture OAuth provider refresh token before Supabase clears the URL hash.
const initialHashParams = typeof window !== 'undefined' ? window.location.hash : '';
let extractedProviderRefreshToken: string | null = null;
if (initialHashParams) {
  try {
    const params = new URLSearchParams(initialHashParams.substring(1));
    extractedProviderRefreshToken = params.get('provider_refresh_token');
  } catch (e) {
    extractedProviderRefreshToken = null;
  }
}

export type UserRole = 'user' | 'admin';

interface Profile {
  id: string;
  email: string;
  full_name: string | null;
  avatar_url: string | null;
  role: UserRole;
  status: 'active' | 'suspended' | 'pending';
  calendar_sync_enabled?: boolean | null;
}

interface AuthContextType {
  session: Session | null;
  user: User | null;
  profile: Profile | null;
  loading: boolean;
  isAdmin: boolean;
  isGoogleLinked: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [session, setSession] = useState<Session | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    const { data: { subscription } } = supabase.auth.onAuthStateChange(async (_event, session) => {
      if (!mounted) return;
      
      setSession(session);
      setUser(session?.user ?? null);
      
      if (session?.user) {
        // Use user metadata for immediate UI updates
        const meta = session.user.user_metadata;
        const initialProfile: Profile = {
          id: session.user.id,
          email: session.user.email || '',
          full_name: meta?.full_name || meta?.name || 'User',
          avatar_url: meta?.avatar_url || meta?.picture || null,
          role: (meta?.role as UserRole) || 'user',
          status: 'active'
        };
        
        setProfile(initialProfile);
        setLoading(false);

        // Sync extra profile details from DB in background
        try {
          const { data, error } = await supabase
            .from('profiles')
            .select('*')
            .eq('id', session.user.id)
            .single();

          if (!error && data && mounted) {
            setProfile(data as Profile);
          }

          let providerRefreshToken = session.provider_refresh_token || extractedProviderRefreshToken;
          if (providerRefreshToken) {
            extractedProviderRefreshToken = null;
          }

          if (providerRefreshToken) {
            const { error: updateError } = await supabase
              .from('profiles')
              .update({ 
                google_refresh_token: providerRefreshToken,
                calendar_sync_enabled: true 
              })
              .eq('id', session.user.id);
            if (updateError) {
              console.error('[AuthContext] Failed to save provider_refresh_token:', updateError);
            } else {
              setProfile((current) => current ? { ...current, calendar_sync_enabled: true } : current);
            }
          }
        } catch (err) {
          console.error('[AuthContext] Background sync failed:', err);
        }
      } else {
        if (mounted) {
          setProfile(null);
          setLoading(false);
        }
      }
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, []);

  const signOut = async () => {
    supabase.auth.signOut().then(({ error }) => {
      if (error) console.error("Supabase background signOut error:", error);
    }).catch(err => {
      console.error("Supabase background signOut failed:", err);
    });

    setSession(null);
    setUser(null);
    setProfile(null);
    setLoading(false);
  };

  const isAdmin = profile?.role === 'admin';
  const isGoogleLinked = Boolean(profile?.calendar_sync_enabled);

  const value = {
    session,
    user,
    profile,
    loading,
    isAdmin,
    isGoogleLinked,
    signOut,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

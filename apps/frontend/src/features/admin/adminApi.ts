import { baseApi, GROUPS_TAG, ROLES_TAG, USERS_TAG } from '../../shared/api/baseApi';

/** DTOs of API_CONTRACTS.md §2.4–2.6. */
export type UserStatus = 'active' | 'disabled' | 'invited';

export interface UserSummary {
  id: string;
  email: string;
  display_name: string;
  status: UserStatus;
  roles: string[];
  created_at: string;
  last_login_at: string | null;
}

export interface UserDetails extends UserSummary {
  groups: { id: string; name: string }[];
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface ActivationLink {
  url: string;
  expires_at: string;
}

export interface CreatedUser {
  user: UserDetails;
  activation: ActivationLink;
}

export interface UserListParams {
  q?: string;
  status?: UserStatus;
  group_id?: string;
  cursor?: string;
  limit?: number;
}

export interface GroupSummary {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
  created_at: string;
}

export interface GroupDetails extends GroupSummary {
  members: { id: string; email: string; display_name: string; status: UserStatus }[];
  roles: string[];
}

export interface Role {
  key: string;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
}

/** Drop empty values so only allowlisted, meaningful query parameters are sent (ADR-014). */
function query(params: object): Record<string, string> {
  const result: Record<string, string> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') result[key] = String(value);
  }
  return result;
}

const userTag = (id: string) => ({ type: USERS_TAG, id }) as const;
const groupTag = (id: string) => ({ type: GROUPS_TAG, id }) as const;

export const adminApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listUsers: build.query<Page<UserSummary>, UserListParams>({
      query: (params) => ({ url: '/users', params: query(params) }),
      providesTags: [USERS_TAG],
    }),
    getUser: build.query<UserDetails, string>({
      query: (id) => `/users/${encodeURIComponent(id)}`,
      providesTags: (_result, _error, id) => [userTag(id)],
    }),
    createUser: build.mutation<CreatedUser, { email: string; display_name: string; roles: string[] }>({
      query: (body) => ({ url: '/users', method: 'POST', body }),
      invalidatesTags: [USERS_TAG],
    }),
    updateUser: build.mutation<
      UserDetails,
      { id: string; display_name?: string; status?: 'active' | 'disabled' }
    >({
      query: ({ id, ...body }) => ({ url: `/users/${encodeURIComponent(id)}`, method: 'PATCH', body }),
      invalidatesTags: (_result, _error, { id }) => [USERS_TAG, userTag(id), GROUPS_TAG],
    }),
    deleteUser: build.mutation<null, string>({
      query: (id) => ({ url: `/users/${encodeURIComponent(id)}`, method: 'DELETE' }),
      invalidatesTags: [USERS_TAG, GROUPS_TAG],
    }),
    setUserRoles: build.mutation<UserDetails, { id: string; roles: string[] }>({
      query: ({ id, roles }) => ({
        url: `/users/${encodeURIComponent(id)}/roles`,
        method: 'PUT',
        body: { roles },
      }),
      invalidatesTags: (_result, _error, { id }) => [USERS_TAG, userTag(id)],
    }),
    revokeUserSessions: build.mutation<{ revoked: number }, string>({
      query: (id) => ({ url: `/users/${encodeURIComponent(id)}/sessions/revoke`, method: 'POST' }),
    }),
    reissueActivation: build.mutation<ActivationLink, string>({
      query: (id) => ({ url: `/users/${encodeURIComponent(id)}/activation`, method: 'POST' }),
    }),
    listGroups: build.query<Page<GroupSummary>, { q?: string; cursor?: string; limit?: number }>({
      query: (params) => ({ url: '/groups', params: query(params) }),
      providesTags: [GROUPS_TAG],
    }),
    getGroup: build.query<GroupDetails, string>({
      query: (id) => `/groups/${encodeURIComponent(id)}`,
      providesTags: (_result, _error, id) => [groupTag(id)],
    }),
    createGroup: build.mutation<GroupDetails, { name: string; description?: string | null }>({
      query: (body) => ({ url: '/groups', method: 'POST', body }),
      invalidatesTags: [GROUPS_TAG],
    }),
    updateGroup: build.mutation<GroupDetails, { id: string; name?: string; description?: string | null }>({
      query: ({ id, ...body }) => ({ url: `/groups/${encodeURIComponent(id)}`, method: 'PATCH', body }),
      invalidatesTags: (_result, _error, { id }) => [GROUPS_TAG, groupTag(id), USERS_TAG],
    }),
    deleteGroup: build.mutation<null, string>({
      query: (id) => ({ url: `/groups/${encodeURIComponent(id)}`, method: 'DELETE' }),
      invalidatesTags: [GROUPS_TAG, USERS_TAG],
    }),
    setGroupMembers: build.mutation<GroupDetails, { id: string; user_ids: string[] }>({
      query: ({ id, user_ids }) => ({
        url: `/groups/${encodeURIComponent(id)}/members`,
        method: 'PUT',
        body: { user_ids },
      }),
      invalidatesTags: (_result, _error, { id }) => [GROUPS_TAG, groupTag(id), USERS_TAG],
    }),
    setGroupRoles: build.mutation<GroupDetails, { id: string; roles: string[] }>({
      query: ({ id, roles }) => ({
        url: `/groups/${encodeURIComponent(id)}/roles`,
        method: 'PUT',
        body: { roles },
      }),
      invalidatesTags: (_result, _error, { id }) => [GROUPS_TAG, groupTag(id)],
    }),
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    listRoles: build.query<{ items: Role[] }, void>({
      query: () => '/roles',
      providesTags: [ROLES_TAG],
    }),
  }),
});

export const {
  useListUsersQuery,
  useGetUserQuery,
  useCreateUserMutation,
  useUpdateUserMutation,
  useDeleteUserMutation,
  useSetUserRolesMutation,
  useRevokeUserSessionsMutation,
  useReissueActivationMutation,
  useListGroupsQuery,
  useGetGroupQuery,
  useCreateGroupMutation,
  useUpdateGroupMutation,
  useDeleteGroupMutation,
  useSetGroupMembersMutation,
  useSetGroupRolesMutation,
  useListRolesQuery,
} = adminApi;

/** The backend returns an absolute link when PUBLIC_URL is set; otherwise make it absolute for copying. */
export function absoluteLink(url: string): string {
  return url.startsWith('/') ? `${window.location.origin}${url}` : url;
}

/**
 * Amplify / Cognito configuration for the SaaS Billing Agent frontend.
 *
 * Values are read from environment variables at build time (Vite injects
 * them via `import.meta.env`).  Provide defaults for local development.
 */

export const amplifyConfig = {
  Auth: {
    Cognito: {
      userPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID ?? "",
      userPoolClientId: import.meta.env.VITE_COGNITO_CLIENT_ID ?? "",
      loginWith: {
        oauth: {
          domain: import.meta.env.VITE_COGNITO_DOMAIN ?? "",
          scopes: ["openid", "profile"],
          redirectSignIn: [import.meta.env.VITE_REDIRECT_SIGN_IN ?? "http://localhost:5173/"],
          redirectSignOut: [import.meta.env.VITE_REDIRECT_SIGN_OUT ?? "http://localhost:5173/"],
          responseType: "code" as const,
        },
      },
    },
  },
};

/** AgentCore Runtime base URL */
export const AGENT_RUNTIME_URL: string =
  import.meta.env.VITE_AGENT_RUNTIME_URL ?? "http://localhost:8080";

/** AgentCore Runtime ARN — used to construct the invoke URL */
export const AGENT_RUNTIME_ARN: string =
  import.meta.env.VITE_AGENT_RUNTIME_ARN ?? "";

/**
 * Cairn on Cloudflare. The Worker is a thin front door: every request goes to the Python API
 * running in a Cloudflare Container, and Cron Triggers run the scheduled jobs in a second,
 * private container. The API code is unchanged. See README.md, "Deploy to Cloudflare".
 */
import { Container, getContainer, getRandom } from "@cloudflare/containers";

type Settings = Record<string, string>;

// Environment variables each container receives. Vars come from wrangler.jsonc, secrets from
// `wrangler secret put`. Anything unset is left out, and the Python side decides what's required.
// Never add CAIRN_DEV_AUTH_SECRET: development sign-in must not be reachable on Cloudflare.
const API_KEYS = [
  "MONGODB_URI",
  "CAIRN_MONGODB_DB",
  "CAIRN_DB_POOL_MIN",
  "CAIRN_DB_POOL_MAX",
  "CAIRN_AUTH0_DOMAIN",
  "CAIRN_AUTH0_AUDIENCE",
  "CAIRN_CLAIM_NAMESPACE",
  "CAIRN_AUTH0_EMAIL_CONNECTION",
  "CAIRN_TERMS_VERSION",
  "CAIRN_PRIVACY_VERSION",
  "CAIRN_PRIVACY_POLICY_URL",
  "CAIRN_TERMS_URL",
  "CAIRN_JOURNEY_MAP_URL",
  "CAIRN_SUPPORT_URL",
  "CAIRN_AI_PROVIDER_NAME",
  "CAIRN_ESTATE_PLAN_MODE",
  "CAIRN_PRE_NEED_PATH",
  "CAIRN_OVERWHELM_SKIP_THRESHOLD",
  "CAIRN_CORS_ORIGINS",
] as const;

// The jobs user's connection string and provider secrets reach only the jobs container.
const JOBS_KEYS = [
  "CAIRN_JOBS_MONGODB_URI",
  "CAIRN_MONGODB_DB",
  "CAIRN_SMTP_HOST",
  "CAIRN_SMTP_PORT",
  "CAIRN_SMTP_USERNAME",
  "CAIRN_SMTP_PASSWORD",
  "CAIRN_EMAIL_FROM",
  "CAIRN_AUTH0_DOMAIN",
  "CAIRN_AUTH0_MGMT_CLIENT_ID",
  "CAIRN_AUTH0_MGMT_CLIENT_SECRET",
  "CAIRN_APPLE_CLIENT_ID",
  "CAIRN_APPLE_TEAM_ID",
  "CAIRN_APPLE_KEY_ID",
  "CAIRN_APPLE_PRIVATE_KEY",
] as const;

// Cron expression (as written in wrangler.jsonc) to the jobs it runs, in order.
// Job names match JOBS in api/cairn_api/jobs.py.
export const JOBS_BY_CRON: Record<string, readonly string[]> = {
  "*/5 * * * *": ["outbound"],
  "*/15 * * * *": ["identity_cleanup"],
  "7 * * * *": ["purge_held_cases", "settle_trial_clocks"],
  "30 3 * * *": ["purge_inactive_drafts", "expire_trials"],
};

function pick(env: Env, keys: readonly string[]): Settings {
  const source = env as unknown as Record<string, unknown>;
  const out: Settings = {};
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value !== "") out[key] = value;
  }
  return out;
}

export class CairnApi extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "15m";
  pingEndpoint = "container/healthz";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    this.envVars = { ...pick(env, API_KEYS), CAIRN_PROCESS: "api", PORT: "8080" };
  }
}

export class CairnJobs extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "5m";
  pingEndpoint = "container/healthz";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    this.envVars = { ...pick(env, JOBS_KEYS), CAIRN_PROCESS: "jobs", PORT: "8080" };
  }
}

async function apiContainer(env: Env) {
  const instances = Number.parseInt(env.CAIRN_API_INSTANCES ?? "1", 10);
  return instances > 1 ? getRandom(env.CAIRN_API, instances) : getContainer(env.CAIRN_API, "api");
}

async function runJob(env: Env, name: string): Promise<void> {
  const jobs = getContainer(env.CAIRN_JOBS, "jobs");
  const started = Date.now();
  try {
    const response = await jobs.fetch(new Request(`http://jobs/jobs/${name}`, { method: "POST" }));
    // The body holds counts and status only, never anything personal.
    const body = await response.text();
    const log = response.ok ? console.log : console.error;
    log(`job ${name} ${response.status} in ${Date.now() - started}ms ${body}`);
  } catch (error) {
    console.error(`job ${name} could not run: ${error instanceof Error ? error.name : "unknown error"}`);
  }
}

export default {
  async fetch(request, env): Promise<Response> {
    const container = await apiContainer(env);
    return container.fetch(request);
  },

  async scheduled(controller, env, ctx): Promise<void> {
    const names = JOBS_BY_CRON[controller.cron];
    if (!names) {
      console.error(`no jobs for cron "${controller.cron}". Update JOBS_BY_CRON in src/index.ts.`);
      return;
    }
    ctx.waitUntil(
      (async () => {
        for (const name of names) await runJob(env, name);
      })(),
    );
  },
} satisfies ExportedHandler<Env>;

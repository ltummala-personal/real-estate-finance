// Cloudflare Worker that runs the Django app (Dockerfile) in a Cloudflare Container and proxies all traffic to it.
import { DurableObject } from "cloudflare:workers";

const PORT = 8000;
const INSTANCE_NAME = "primary";
const INACTIVITY_TIMEOUT_MS = 15 * 60 * 1000;
const READY_ATTEMPTS = 300;

// Worker vars/secrets copied into the container's environment when it starts.
const PASSTHROUGH = [
  "DJANGO_SECRET_KEY",
  "DATABASE_URL",
  "DJANGO_HSTS_SECONDS",
  "DOCUMENT_MAX_UPLOAD_MB",
  "R2_ACCOUNT_ID",
  "R2_BUCKET",
  "R2_ACCESS_KEY_ID",
  "R2_SECRET_ACCESS_KEY",
  "DJANGO_SUPERUSER_USERNAME",
  "DJANGO_SUPERUSER_PASSWORD",
  "DJANGO_SUPERUSER_EMAIL",
];

export class DjangoContainer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    const container = ctx.container;
    if (container?.running) {
      void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
      this.observeExit();
    }
  }

  async fetch(request) {
    const incoming = new URL(request.url);
    this.starting ??= this.startAndWaitForPort(incoming.host).finally(() => {
      this.starting = undefined;
    });
    await this.starting;

    const url = new URL(request.url);
    url.protocol = "http:";
    url.host = "container";
    const forwarded = new Request(url, request);
    forwarded.headers.delete("host");
    // Overwrite (never append) so clients cannot spoof the host/scheme Django sees.
    forwarded.headers.set("X-Forwarded-Host", incoming.host);
    forwarded.headers.set("X-Forwarded-Proto", incoming.protocol.replace(":", ""));
    const clientIp = request.headers.get("CF-Connecting-IP");
    if (clientIp) forwarded.headers.set("X-Forwarded-For", clientIp);
    return this.ctx.container.getTcpPort(PORT).fetch(forwarded);
  }

  containerEnv(requestHost) {
    const hosts = (this.env.APP_HOSTNAMES || requestHost)
      .split(",")
      .map((host) => host.trim())
      .filter(Boolean);
    const env = {
      DJANGO_ALLOWED_HOSTS: [...hosts, "container"].join(","),
      DJANGO_CSRF_TRUSTED_ORIGINS: hosts.map((host) => `https://${host}`).join(","),
      DJANGO_SECURE: "true",
      DJANGO_USE_X_FORWARDED_HOST: "true",
      DJANGO_DEBUG: "False",
    };
    for (const name of PASSTHROUGH) {
      if (typeof this.env[name] === "string" && this.env[name] !== "") env[name] = this.env[name];
    }
    if (!env.DJANGO_SECRET_KEY || !env.DATABASE_URL) {
      throw new Error("Set the DJANGO_SECRET_KEY and DATABASE_URL secrets with `wrangler secret put`.");
    }
    return env;
  }

  async startAndWaitForPort(requestHost) {
    const container = this.ctx.container;
    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: this.env.CONTAINER_INSTANCE || "standard-1",
        enableInternet: true,
        env: this.containerEnv(requestHost),
      });
    }
    this.observeExit();
    await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);

    // Cold start runs migrations before Gunicorn listens, so allow a generous wait.
    const port = container.getTcpPort(PORT);
    let lastError;
    for (let attempt = 0; attempt < READY_ATTEMPTS; attempt++) {
      try {
        const response = await port.fetch("http://container/healthz/", { signal: AbortSignal.timeout(2000) });
        await response.body?.cancel();
        if (response.ok) return;
        lastError = new Error(`Health check returned ${response.status}`);
      } catch (error) {
        lastError = error;
      }
      await scheduler.wait(500);
    }
    throw new Error(`Container did not become ready on port ${PORT}`, { cause: lastError });
  }

  observeExit() {
    if (this.monitoring) return;
    this.monitoring = this.ctx.container
      .monitor()
      .then(() => console.log("Container exited"))
      .catch((error) => console.error("Container failed:", error))
      .finally(() => {
        this.monitoring = undefined;
      });
    this.ctx.waitUntil(this.monitoring);
  }
}

export default {
  async fetch(request, env) {
    try {
      return await env.APP.getByName(INSTANCE_NAME).fetch(request);
    } catch (error) {
      console.error("Request failed:", error);
      return new Response("The application is starting or unavailable. Please retry in a minute.", {
        status: 503,
        headers: { "Retry-After": "30", "Content-Type": "text/plain" },
      });
    }
  },
};

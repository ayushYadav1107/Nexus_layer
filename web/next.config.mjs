const BACKEND = process.env.BACKEND_URL || "http://127.0.0.1:8000";

/** Proxy the API through Next so the browser only ever talks to one origin.
 *  Cheaper than configuring CORS and keeps the backend off the public net. */
export default {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/:path*` }];
  },
};

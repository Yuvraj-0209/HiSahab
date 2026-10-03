/* Named aliases over the generated OpenAPI types (src/api/schema.d.ts, built from the committed
 * snapshot src/api/openapi.json by `npm run api:types`).
 *
 * Every money field in these types is `string` -- and `string | null` where null carries
 * meaning (declared_cash, gap, variance, credit_limit ...). The compiler therefore refuses the
 * two classic mistakes: adding money as numbers, and passing a nullable figure somewhere that
 * assumes it is always present. Nothing here re-declares a shape by hand; a hand-written copy
 * is a copy that drifts from the server.
 */

import type { components } from "./schema";

export type Schemas = components["schemas"];

export type Me = Schemas["MeResponse"];
export type ClientConfig = Schemas["ClientConfigResponse"];

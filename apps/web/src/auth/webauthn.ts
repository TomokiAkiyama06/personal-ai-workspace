// The browser half of a passkey ceremony (Decision 0025). The Backend sends the
// JSON options of `navigator.credentials.create()` / `get()` (binary values in
// base64url) and expects the `toJSON()` form of the PublicKeyCredential back.
// Browsers that have the JSON helpers use them; others get the same conversion here.
import { ApiError } from "../api/client";

type Json = Record<string, unknown>;

export function base64urlToBuffer(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function bufferToBase64url(buffer: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    buffer instanceof ArrayBuffer
      ? new Uint8Array(buffer)
      : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

interface JsonCapableCredentialStatic {
  parseCreationOptionsFromJSON?: (options: Json) => PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON?: (options: Json) => PublicKeyCredentialRequestOptions;
}

function credentialStatic(): JsonCapableCredentialStatic | null {
  if (typeof window === "undefined" || typeof window.PublicKeyCredential === "undefined") {
    return null;
  }
  return window.PublicKeyCredential as unknown as JsonCapableCredentialStatic;
}

export function webauthnSupported(): boolean {
  return credentialStatic() !== null && typeof navigator.credentials?.create === "function";
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (!Array.isArray(list)) return undefined;
  return list.map((item: Json) => ({
    ...(item as object),
    type: "public-key",
    id: base64urlToBuffer(String(item.id)),
  })) as PublicKeyCredentialDescriptor[];
}

export function creationOptionsFromJSON(options: Json): PublicKeyCredentialCreationOptions {
  const helper = credentialStatic()?.parseCreationOptionsFromJSON;
  if (helper) return helper(options);
  const user = options.user as Json;
  return {
    ...(options as object),
    challenge: base64urlToBuffer(String(options.challenge)),
    user: { ...(user as object), id: base64urlToBuffer(String(user.id)) },
    excludeCredentials: descriptors(options.excludeCredentials),
  } as PublicKeyCredentialCreationOptions;
}

export function requestOptionsFromJSON(options: Json): PublicKeyCredentialRequestOptions {
  const helper = credentialStatic()?.parseRequestOptionsFromJSON;
  if (helper) return helper(options);
  return {
    ...(options as object),
    challenge: base64urlToBuffer(String(options.challenge)),
    allowCredentials: descriptors(options.allowCredentials),
  } as PublicKeyCredentialRequestOptions;
}

/** The `toJSON()` form of a credential (registration or assertion). */
export function credentialToJSON(credential: PublicKeyCredential): Json {
  const native = (credential as unknown as { toJSON?: () => Json }).toJSON;
  if (typeof native === "function") return native.call(credential);
  const response = credential.response;
  const json: Json = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    clientExtensionResults: credential.getClientExtensionResults?.() ?? {},
  };
  if (credential.authenticatorAttachment) {
    json.authenticatorAttachment = credential.authenticatorAttachment;
  }
  if ("attestationObject" in response) {
    const attestation = response as AuthenticatorAttestationResponse;
    json.response = {
      clientDataJSON: bufferToBase64url(attestation.clientDataJSON),
      attestationObject: bufferToBase64url(attestation.attestationObject),
      ...(typeof attestation.getTransports === "function"
        ? { transports: attestation.getTransports() }
        : {}),
    };
  } else {
    const assertion = response as AuthenticatorAssertionResponse;
    json.response = {
      clientDataJSON: bufferToBase64url(assertion.clientDataJSON),
      authenticatorData: bufferToBase64url(assertion.authenticatorData),
      signature: bufferToBase64url(assertion.signature),
      ...(assertion.userHandle ? { userHandle: bufferToBase64url(assertion.userHandle) } : {}),
    };
  }
  return json;
}

function ceremonyError(error: unknown): ApiError {
  // NotAllowedError: the user cancelled or the prompt timed out (the browser does
  // not tell which, on purpose).
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return new ApiError(0, "webauthn_cancelled", "The passkey operation was cancelled");
  }
  if (error instanceof DOMException && error.name === "InvalidStateError") {
    return new ApiError(0, "passkey_exists", "That credential is already registered");
  }
  return new ApiError(0, "passkey_rejected", "The passkey operation failed");
}

export async function createPasskey(options: Json): Promise<Json> {
  if (!webauthnSupported()) throw new ApiError(0, "webauthn_unsupported", "No WebAuthn");
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.create({
      publicKey: creationOptionsFromJSON(options),
    });
  } catch (error) {
    throw ceremonyError(error);
  }
  if (!credential) throw new ApiError(0, "webauthn_cancelled", "No credential");
  return credentialToJSON(credential as PublicKeyCredential);
}

export async function getPasskey(options: Json): Promise<Json> {
  if (!webauthnSupported()) throw new ApiError(0, "webauthn_unsupported", "No WebAuthn");
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.get({ publicKey: requestOptionsFromJSON(options) });
  } catch (error) {
    throw ceremonyError(error);
  }
  if (!credential) throw new ApiError(0, "webauthn_cancelled", "No credential");
  return credentialToJSON(credential as PublicKeyCredential);
}

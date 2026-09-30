import { afterEach, describe, expect, it, vi } from "vitest";
import {
  base64urlToBuffer,
  bufferToBase64url,
  createPasskey,
  creationOptionsFromJSON,
  credentialToJSON,
  requestOptionsFromJSON,
} from "./webauthn";

afterEach(() => {
  vi.unstubAllGlobals();
});

const bytes = (buffer: ArrayBuffer | BufferSource) =>
  Array.from(new Uint8Array(buffer as ArrayBuffer));

describe("base64url", () => {
  it("round-trips binary values without padding", () => {
    const original = new Uint8Array([0, 1, 250, 251, 252, 253, 254, 255]);
    const encoded = bufferToBase64url(original.buffer);
    expect(encoded).not.toMatch(/[+/=]/);
    expect(bytes(base64urlToBuffer(encoded))).toEqual(Array.from(original));
  });
});

describe("options conversion (browsers without the JSON helpers)", () => {
  it("decodes the challenge, the user id and the excluded credentials", () => {
    const options = creationOptionsFromJSON({
      challenge: bufferToBase64url(new Uint8Array([1, 2, 3]).buffer),
      rp: { id: "localhost", name: "PAW" },
      user: { id: bufferToBase64url(new Uint8Array([9]).buffer), name: "t", displayName: "t" },
      pubKeyCredParams: [{ type: "public-key", alg: -7 }],
      excludeCredentials: [
        { id: bufferToBase64url(new Uint8Array([7]).buffer), type: "public-key" },
      ],
    });
    expect(bytes(options.challenge)).toEqual([1, 2, 3]);
    expect(bytes(options.user.id)).toEqual([9]);
    expect(bytes(options.excludeCredentials?.[0]?.id as BufferSource)).toEqual([7]);
    expect(options.rp.id).toBe("localhost");
  });

  it("decodes the allowed credentials of an authentication", () => {
    const options = requestOptionsFromJSON({
      challenge: bufferToBase64url(new Uint8Array([4]).buffer),
      allowCredentials: [{ id: bufferToBase64url(new Uint8Array([5]).buffer), type: "public-key" }],
      userVerification: "required",
    });
    expect(bytes(options.challenge)).toEqual([4]);
    expect(bytes(options.allowCredentials?.[0]?.id as BufferSource)).toEqual([5]);
    expect(options.userVerification).toBe("required");
  });
});

describe("credentialToJSON", () => {
  it("uses the browser's toJSON when there is one", () => {
    const credential = { toJSON: () => ({ id: "native" }) } as unknown as PublicKeyCredential;
    expect(credentialToJSON(credential)).toEqual({ id: "native" });
  });

  it("encodes an assertion by hand otherwise", () => {
    const buffer = (value: number) => new Uint8Array([value]).buffer;
    const credential = {
      id: "cred",
      rawId: buffer(1),
      type: "public-key",
      authenticatorAttachment: "platform",
      getClientExtensionResults: () => ({}),
      response: {
        clientDataJSON: buffer(2),
        authenticatorData: buffer(3),
        signature: buffer(4),
        userHandle: buffer(5),
      },
    } as unknown as PublicKeyCredential;
    expect(credentialToJSON(credential)).toEqual({
      id: "cred",
      rawId: "AQ",
      type: "public-key",
      authenticatorAttachment: "platform",
      clientExtensionResults: {},
      response: {
        clientDataJSON: "Ag",
        authenticatorData: "Aw",
        signature: "BA",
        userHandle: "BQ",
      },
    });
  });
});

describe("createPasskey", () => {
  it("reports a browser without WebAuthn", async () => {
    vi.stubGlobal("PublicKeyCredential", undefined);
    await expect(createPasskey({})).rejects.toMatchObject({ code: "webauthn_unsupported" });
  });

  it("reports a cancelled prompt", async () => {
    vi.stubGlobal("PublicKeyCredential", function PublicKeyCredential() {});
    vi.stubGlobal("navigator", {
      credentials: {
        create: vi.fn(async () => {
          throw new DOMException("cancelled", "NotAllowedError");
        }),
      },
    });
    await expect(
      createPasskey({
        challenge: "AQ",
        user: { id: "AQ", name: "t", displayName: "t" },
        rp: { name: "x" },
        pubKeyCredParams: [],
      }),
    ).rejects.toMatchObject({ code: "webauthn_cancelled" });
  });
});

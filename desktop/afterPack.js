// afterPack hook — ad-hoc code-sign the macOS bundle so it LAUNCHES on modern macOS.
//
// HISTORY (why this is the way it is)
// -----------------------------------
// electron-builder is configured with mac.identity = null → it SKIPS signing. It still
// renames the main exe + helpers and rewrites Info.plist, which invalidates the Electron
// prebuilt's signatures, so the bundle must be re-signed here or it ships broken.
//
//   * 2.2.14: not re-signed at all → "damaged / code does not match original signed code".
//   * 2.2.15: re-signed ad-hoc, but WITHOUT the disable-library-validation entitlement and
//     with hardenedRuntime:false. On macOS 26 (M1) it CRASHED AT LAUNCH:
//       "Electron Framework ... not valid for use in process: ... different Team IDs".
//     That is Apple Silicon's team-ID enforcement on images mapped into the process — the
//     ad-hoc components don't share a coherent team identity, so dyld refuses the framework.
//
// THE FIX (free tier — no Apple Developer ID)
// -------------------------------------------
// Re-sign the WHOLE bundle inside-out, ad-hoc ("-"), applying entitlements.mac.plist —
// whose com.apple.security.cs.disable-library-validation key tells the loader to allow
// loading code that isn't signed with a matching team ID. Applied to EVERY file (via
// optionsForFile) so the main exe, the Electron Framework, and all helpers are coherent.
// hardenedRuntime:true is the documented pairing for this entitlement.
//
// This is a BRIDGE, not the strategy: ad-hoc is officially "build-machine only / unsuitable
// for distribution" and Apple tightens it every release (26 already broke 2.2.15). The real
// fix is Apple Developer ID + notarization (see MAC-DISTRIBUTION-BUILD-PLAN.md Phase D),
// deferred until a beta tester confirms the app works.
//
// VERIFICATION: CI no longer trusts `codesign --verify` (that only checks the static seal and
// gave a false green on the crashing 2.2.15). mac-build.yml now LAUNCHES the binary
// (ELECTRON_RUN_AS_NODE) so a dyld/signing crash fails the build.

const { signAsync } = require('@electron/osx-sign');
const path = require('path');

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const appName = context.packager.appInfo.productFilename; // "the parent program"
  const appPath = path.join(context.appOutDir, `${appName}.app`);
  const entitlements = path.join(__dirname, 'entitlements.mac.plist');

  console.log(`[afterPack] ad-hoc signing ${appPath} (arch=${context.arch})`);
  console.log(`[afterPack] entitlements: ${entitlements}`);
  await signAsync({
    app: appPath,
    identity: '-', // ad-hoc signature — no Developer cert required
    identityValidation: false, // '-' is not a keychain identity; skip the lookup
    platform: 'darwin',
    hardenedRuntime: true, // documented pairing for disable-library-validation
    entitlements, // top-level app
    entitlementsInherit: entitlements, // nested helpers
    preAutoEntitlements: false, // an ad-hoc identity has no team ID to derive
    // Apply the SAME entitlements + hardened runtime to EVERY signed file so the whole
    // bundle is coherent — this is what makes the framework loadable (fixes the 2.2.15 crash).
    optionsForFile: () => ({ entitlements, hardenedRuntime: true }),
  });
  console.log('[afterPack] ad-hoc signing complete');
};

/** Type augmentation that makes ``t('common.save')`` typo-proof.
 *
 * react-i18next reads the resource type from this declaration and uses
 * it to constrain ``t(...)``'s key argument. When you add a new key to
 * ./zh.ts you must mirror it in ./en.ts — TS catches the asymmetry at
 * build time because both share this exact same shape.
 */

import "react-i18next";

import zh from "./zh";

declare module "react-i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: {
      translation: typeof zh;
    };
    returnNull: false;
  }
}

export {};

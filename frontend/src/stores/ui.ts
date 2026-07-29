import { defineStore } from "pinia";

export const useUiStore = defineStore("ui", {
  state: () => ({
    navOpen: false,
    toast: "",
    toastTimer: 0 as number,
  }),
  actions: {
    notify(message: string) {
      this.toast = message;
      window.clearTimeout(this.toastTimer);
      this.toastTimer = window.setTimeout(() => { this.toast = ""; }, 3200);
    },
  },
});

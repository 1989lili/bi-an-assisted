import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";
import Vant from "vant";
import "vant/lib/index.css";

import App from "./App.vue";
import SignalFeed from "./views/SignalFeed.vue";
import Watchlist from "./views/Watchlist.vue";
import Positions from "./views/Positions.vue";
import Review from "./views/Review.vue";
import Settings from "./views/Settings.vue";

// hash 路由：后端静态托管无需重写规则
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/signals" },
    { path: "/signals", component: SignalFeed, meta: { title: "信号" } },
    { path: "/watchlist", component: Watchlist, meta: { title: "自选" } },
    { path: "/positions", component: Positions, meta: { title: "持仓" } },
    { path: "/review", component: Review, meta: { title: "复盘" } },
    { path: "/settings", component: Settings, meta: { title: "设置" } },
  ],
});

createApp(App).use(Vant).use(router).mount("#app");

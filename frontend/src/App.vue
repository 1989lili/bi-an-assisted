<template>
  <div class="app-shell">
    <div class="page-body">
      <router-view />
    </div>
    <van-tabbar route fixed placeholder safe-area-inset-bottom class="tabbar">
      <van-tabbar-item replace to="/signals" icon="volume-o">信号</van-tabbar-item>
      <van-tabbar-item replace to="/watchlist" icon="star-o">自选</van-tabbar-item>
      <van-tabbar-item replace to="/positions" icon="balance-o">持仓</van-tabbar-item>
      <van-tabbar-item replace to="/review" icon="chart-trending-o">复盘</van-tabbar-item>
      <van-tabbar-item replace to="/settings" icon="setting-o">设置</van-tabbar-item>
    </van-tabbar>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  padding-bottom: 8px;
}
.page-body {
  padding: 10px 12px 60px;
}
.tabbar {
  background: #1c1c1e;
}
</style>

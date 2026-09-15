-- crawlerToBase 运行批次心跳与持久化实时日志
-- 执行一次即可；用于自动收束失去后台线程的“假运行”批次，并支持刷新后回放最近日志。

ALTER TABLE `policy_crawler_run`
  ADD COLUMN `heartbeat_at` datetime DEFAULT NULL COMMENT '最近一次任务进度心跳' AFTER `finished_at`;

UPDATE `policy_crawler_run`
SET `heartbeat_at` = COALESCE(`finished_at`, `started_at`, `created_at`)
WHERE `heartbeat_at` IS NULL;

ALTER TABLE `policy_crawler_run`
  ADD KEY `idx_policy_crawler_run_heartbeat` (`status`,`heartbeat_at`);

CREATE TABLE IF NOT EXISTS `policy_crawler_run_event` (
  `run_event_id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '全局事件序号，SSE 续传游标',
  `run_id` char(32) NOT NULL COMMENT '批次ID',
  `event_type` varchar(32) NOT NULL COMMENT 'status/log/progress/complete/error/warning',
  `message` text NOT NULL COMMENT '页面展示消息',
  `data_json` longtext DEFAULT NULL COMMENT '事件附加数据JSON',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`run_event_id`),
  KEY `idx_policy_crawler_run_event_run` (`run_id`,`run_event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='政策爬虫持久化实时日志';

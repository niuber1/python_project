CREATE TABLE IF NOT EXISTS `policy_crawler_schedule_config` (
  `config_id` tinyint NOT NULL COMMENT '固定为 1 的单例配置',
  `enabled` decimal(1,0) NOT NULL DEFAULT 1 COMMENT '是否启用定时抓取',
  `schedule_hour` tinyint NOT NULL COMMENT '每天执行小时（0-23）',
  `schedule_minute` tinyint NOT NULL COMMENT '每天执行分钟（0-59）',
  `task_codes_json` text NOT NULL COMMENT '参与定时抓取的任务编码',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`config_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='政策爬虫定时抓取配置';

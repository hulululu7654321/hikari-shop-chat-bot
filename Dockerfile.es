FROM docker.elastic.co/elasticsearch/elasticsearch:8.13.0

# 安装 IK 中文分词插件
RUN elasticsearch-plugin install --batch \
    https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.13.0.zip

ENV TZ=Asia/Shanghai

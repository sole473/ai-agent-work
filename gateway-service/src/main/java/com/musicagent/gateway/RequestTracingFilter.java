package com.musicagent.gateway;

import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.cloud.gateway.filter.GatewayFilterChain;
import org.springframework.cloud.gateway.filter.GlobalFilter;
import org.springframework.cloud.gateway.route.Route;
import org.springframework.core.Ordered;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ServerWebExchange;
import org.springframework.cloud.gateway.support.ServerWebExchangeUtils;
import reactor.core.publisher.Mono;

@Component
public class RequestTracingFilter implements GlobalFilter, Ordered {
    private static final Logger logger = LoggerFactory.getLogger(RequestTracingFilter.class);
    private static final String REQUEST_ID = "X-Request-ID";

    @Override
    public int getOrder() { return Ordered.HIGHEST_PRECEDENCE; }

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, GatewayFilterChain chain) {
        String requestId = UUID.randomUUID().toString().replace("-", "");
        long startedAt = System.nanoTime();
        var request = exchange.getRequest().mutate()
            .headers(headers -> headers.set(REQUEST_ID, requestId))
            .build();
        ServerWebExchange tracedExchange = exchange.mutate().request(request).build();
        tracedExchange.getResponse().getHeaders().set(REQUEST_ID, requestId);

        return chain.filter(tracedExchange).doFinally(signal -> {
            Route route = tracedExchange.getAttribute(ServerWebExchangeUtils.GATEWAY_ROUTE_ATTR);
            String routeId = route == null ? null : route.getId();
            String userId = tracedExchange.getAttribute("music.authenticatedUserId");
            Integer status = tracedExchange.getResponse().getStatusCode() == null
                ? null : tracedExchange.getResponse().getStatusCode().value();
            logger.info("event=gateway_request request_id={} method={} route={} status={} user_id={} duration_ms={} signal={}",
                requestId,
                tracedExchange.getRequest().getMethod(),
                routeId,
                status,
                userId,
                (System.nanoTime() - startedAt) / 1_000_000,
                signal);
        });
    }
}

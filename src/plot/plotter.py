import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np



def plot_system(mas, goal_state, avg_goal_dist, cost_val):
    fstate = mas.full_system_state
    cluster_centroids = mas.cluster_centroids
    n_clusters = mas.n_clusters
    clust_labels = mas.clust_labels
    fig, ax = plt.subplots(figsize=(4, 4), dpi=140)
    ax.scatter(goal_state[0], goal_state[1], s=30, c='k', marker='x')
    colors = cm.rainbow(np.linspace(0, 1, n_clusters))
    for cdx in range(n_clusters):
        agent_indices = np.where(clust_labels == cdx)[0]
        ax.scatter(fstate[agent_indices, 0], fstate[agent_indices, 1], 
                    s=5, c=colors[cdx], marker='.')
        ax.scatter(cluster_centroids[cdx][0], cluster_centroids[cdx][1], 
                    s=40, facecolors='none', edgecolors='#000000', marker='o')
    ax.set_title(f"Avg goal dist: {avg_goal_dist:.2f}; cost: {cost_val:.2f}")
    ax.set_xlim(-15, goal_state[0] * 1.2)
    ax.set_ylim(-10, 10)
    plt.show() 